import json
import logging
import re
from subprocess import check_output

import git
import requests
from django.conf import settings
from django.db import models
from django.db.models import Q
from django.db.models.functions import Length
from django.db.utils import DataError
from django.template.loader import get_template
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.safestring import mark_safe

from autoslug import AutoSlugField
from autoslug.utils import slugify
from ndh.models import Links, NamedModel, TimeStampedModel
from ndh.utils import enum_to_choices, query_sum

from .utils import SOURCES, api_next, invalid_mail, slugify_with_dots, valid_name

logger = logging.getLogger('rainboard.models')

MAIN_BRANCHES = ['master', 'devel']
RPKG_URL = 'http://robotpkg.openrobots.org'
DOC_URL = 'https://gepettoweb.laas.fr/doc'
RPKG_LICENSES = {
    'gnu-lgpl-v3': 'LGPL-3.0',
    'gnu-lgpl-v2': 'LGPL-2.0',
    'gnu-lgpl-v2.1': 'LGPL-2.1',
    'mit': 'MIT',
    'gnu-gpl-v3': 'GPL-3.0',
    '2-clause-bsd': 'BSD-2-Clause',
    'eclipse': 'EPL-1.0',
    'modified-bsd': 'BSD-3-Clause'
}
RPKG_FIELDS = ['PKGBASE', 'PKGVERSION', 'MASTER_SITES', 'MASTER_REPOSITORY', 'MAINTAINER', 'COMMENT', 'HOMEPAGE']
CMAKE_FIELDS = {
    'NAME': 'cmake_name',
    'DESCRIPTION': 'description',
    'URL': 'homepage',
    'VERSION': 'version',
    'SUFFIX': 'suffix'
}
TRAVIS_STATE = {'created': None, 'passed': True, 'started': None, 'failed': False, 'errored': False, 'canceled': False}
GITLAB_STATUS = {'failed': False, 'success': True, 'pending': None, 'skipped': None, 'canceled': None, 'running': None}


class Namespace(NamedModel):
    group = models.BooleanField(default=False)


class License(models.Model):
    name = models.CharField(max_length=200)
    spdx_id = models.CharField(max_length=50, unique=True)
    url = models.URLField(max_length=200)

    def __str__(self):
        return self.spdx_id or self.name


class Forge(Links, NamedModel):
    source = models.PositiveSmallIntegerField(choices=enum_to_choices(SOURCES))
    url = models.URLField(max_length=200)
    token = models.CharField(max_length=50, blank=True, null=True)
    verify = models.BooleanField(default=True)

    def get_absolute_url(self):
        return self.url

    def api_req(self, url='', name=None, page=1):
        logger.debug(f'requesting api {self} {url}, page {page}')
        try:
            return requests.get(self.api_url() + url, {'page': page}, verify=self.verify, headers=self.headers())
        except requests.exceptions.ConnectionError:
            logger.error(f'requesting api {self} {url}, page {page} - SECOND TRY')
            return requests.get(self.api_url() + url, {'page': page}, verify=self.verify, headers=self.headers())

    def api_data(self, url=''):
        req = self.api_req(url)
        return req.json() if req.status_code == 200 else []  # TODO

    def api_list(self, url='', name=None):
        page = 1
        while page:
            req = self.api_req(url, name, page)
            if req.status_code != 200:
                return []  # TODO
            data = req.json()
            if name is not None:
                data = data[name]
            yield from data
            page = api_next(self.source, req)

    def headers(self):
        return {
            SOURCES.github: {
                'Authorization': f'token {self.token}',
                'Accept': 'application/vnd.github.drax-preview+json'
            },
            SOURCES.gitlab: {
                'Private-Token': self.token
            },
            SOURCES.redmine: {
                'X-Redmine-API-Key': self.token
            },
            SOURCES.travis: {
                'Authorization': f'token {self.token}',
                'TRAVIS-API-Version': '3'
            },
        }[self.source]

    def api_url(self):
        return {
            SOURCES.github: 'https://api.github.com',
            SOURCES.gitlab: f'{self.url}/api/v4',
            SOURCES.redmine: self.url,
            SOURCES.travis: 'https://api.travis-ci.org',
        }[self.source]

    def get_namespaces_github(self):
        for namespace in Namespace.objects.filter(group=True):
            for data in self.api_list(f'/orgs/{namespace.slug}/members'):
                Namespace.objects.get_or_create(slug=data['login'].lower(),
                                                defaults={
                                                    'name': data['login'],
                                                    'group': False
                                                })

    def get_namespaces_gitlab(self):
        for data in self.api_list('/namespaces'):
            Namespace.objects.get_or_create(slug=slugify(data['path']),
                                            defaults={
                                                'name': data['name'],
                                                'group': data['kind'] == 'group'
                                            })
        for data in self.api_list('/users'):
            Namespace.objects.get_or_create(slug=slugify(data['username']), defaults={'name': data['name']})

    def get_namespaces_redmine(self):
        pass  # TODO

    def get_namespaces_travis(self):
        pass

    def get_projects(self):
        getattr(self, f'get_namespaces_{self.get_source_display()}')()
        return getattr(self, f'get_projects_{self.get_source_display()}')()

    def get_projects_github(self):
        for org in Namespace.objects.filter(group=True):
            for data in self.api_list(f'/orgs/{org.slug}/repos'):
                update_github(self, org, data)
        for user in Namespace.objects.filter(group=False):
            for data in self.api_list(f'/users/{user.slug}/repos'):
                if Project.objects.filter(name=valid_name(data['name'])).exists():
                    update_github(self, user, data)

    def get_projects_gitlab(self):
        for data in self.api_list('/projects'):
            update_gitlab(self, data)

        for orphan in Project.objects.filter(main_namespace=None):
            repo = orphan.repo_set.filter(forge__source=SOURCES.gitlab).first()
            if repo:
                update_gitlab(self, self.api_data(f'/projects/{repo.forked_from}'))

    def get_projects_redmine(self):
        pass  # TODO

    def get_projects_travis(self):
        for namespace in Namespace.objects.all():
            for repository in self.api_list(f'/owner/{namespace.slug}/repos', 'repositories'):
                if repository['active']:
                    update_travis(namespace, repository)


class Project(Links, NamedModel, TimeStampedModel):
    public = models.BooleanField(default=True)
    main_namespace = models.ForeignKey(Namespace, on_delete=models.SET_NULL, null=True, blank=True)
    main_forge = models.ForeignKey(Forge, on_delete=models.SET_NULL, null=True, blank=True)
    license = models.ForeignKey(License, on_delete=models.SET_NULL, blank=True, null=True)
    homepage = models.URLField(max_length=200, blank=True, null=True)
    description = models.TextField(blank=True, null=True)
    version = models.CharField(max_length=20, blank=True, null=True)
    updated = models.DateTimeField(blank=True, null=True)
    tests = models.BooleanField(default=True)
    docs = models.BooleanField(default=True)
    from_gepetto = models.BooleanField(default=False)
    cmake_name = models.CharField(max_length=200, blank=True, null=True)
    archived = models.BooleanField(default=False)
    suffix = models.CharField(max_length=50, default='', blank=True)
    allow_format_failure = models.BooleanField(default=True)
    has_python = models.BooleanField(default=True)

    def save(self, *args, **kwargs):
        self.name = valid_name(self.name)
        super().save(*args, **kwargs)

    def git_path(self):
        return settings.RAINBOARD_GITS / self.main_namespace.slug / self.slug.strip()  # workaround SafeText TypeError

    def git(self):
        path = self.git_path()
        if not path.exists():
            logger.info(f'Creating repo for {self.main_namespace.slug}/{self.slug}')
            return git.Repo.init(path)
        return git.Repo(str(path / '.git'))

    def main_repo(self):
        forge = self.main_forge if self.main_forge else get_default_forge(self)
        repo, created = Repo.objects.get_or_create(forge=forge,
                                                   namespace=self.main_namespace,
                                                   project=self,
                                                   defaults={
                                                       'name': self.name,
                                                       'default_branch': 'master',
                                                       'repo_id': 0
                                                   })
        if created:
            repo.api_update()
        return repo

    def update_branches(self, main=True, pull=True):
        branches = [b[2:] for b in self.git().git.branch('-a', '--no-color').split('\n')]
        if main:
            branches = [b for b in branches if b.endswith('master') or b.endswith('devel')]
        for branch in branches:
            logger.info(f'update branch {branch}')
            if branch.startswith('remotes/'):
                branch = branch[8:]
            if branch.count('/') < 2:
                if branch != 'master':
                    logger.error(f'wrong branch "{branch}" in {self.git_path()}')
                continue
            forge, namespace, name = branch.split('/', maxsplit=2)
            namespace, _ = Namespace.objects.get_or_create(slug=slugify(namespace), defaults={'name': namespace})
            forge = Forge.objects.get(slug=forge)
            repo, created = Repo.objects.get_or_create(forge=forge,
                                                       namespace=namespace,
                                                       project=self,
                                                       defaults={
                                                           'name': self.name,
                                                           'default_branch': 'master',
                                                           'repo_id': 0
                                                       })
            if created:
                repo.api_update()
            instance, bcreated = Branch.objects.get_or_create(name=branch, project=self, repo=repo)
            if bcreated:
                instance.update(pull=pull)

    def checkout(self):
        self.main_branch().git().checkout()

    def main_branch(self):
        return self.main_repo().main_branch()

    def cmake(self):
        filename = self.git_path() / 'CMakeLists.txt'
        if not filename.exists():
            return
        with filename.open() as f:
            content = f.read()
        for key, value in CMAKE_FIELDS.items():
            search = re.search(r'set\s*\(\s*project_%s\s+([^)]+)*\)' % key, content, re.I)
            if search:
                try:
                    old = getattr(self, value)
                    new = search.groups()[0].strip(''' \r\n\t'"''').replace('_', '-')
                    if old != new:
                        setattr(self, value, new)
                        self.save()
                except DataError:
                    setattr(self, value, old)
        for dependency in re.findall(r'ADD_[A-Z]+_DEPENDENCY\s*\(["\']?([^ "\')]+).*["\']?\)', content, re.I):
            project = Project.objects.filter(name=valid_name(dependency))
            if project.exists():
                dependency, _ = Dependency.objects.get_or_create(project=self, library=project.first())
                if not dependency.cmake:
                    dependency.cmake = True
                    dependency.save()

    def ros(self):
        try:
            filename = self.git_path() / 'package.xml'
        except TypeError:
            return
        if not filename.exists():
            return
        with filename.open() as f:
            content = f.read()
        for dependency in re.findall(r'<run_depend>(\w+).*</run_depend>', content, re.I):
            project = Project.objects.filter(name=valid_name(dependency))
            if project.exists():
                dependency, _ = Dependency.objects.get_or_create(project=self, library=project.first())
                if not dependency.ros:
                    dependency.ros = True
                    dependency.save()

    def repos(self):
        return self.repo_set.count()

    def rpkgs(self):
        return self.robotpkg_set.count()

    def update_tags(self):
        for tag in self.git().tags:
            Tag.objects.get_or_create(name=str(tag), project=self)

    def update_repo(self):
        branch = str(self.main_branch()).split('/', maxsplit=2)[2]
        self.git().head.commit = self.git().remotes[self.main_repo().git_remote()].refs[branch].commit

    def ci_jobs(self):
        if self.main_forge.source == SOURCES.gitlab:
            self.main_repo().get_jobs_gitlab()

    def update(self, only_main_branches=True):
        if self.main_namespace is None:
            return
        self.update_branches(main=only_main_branches)
        self.update_tags()
        self.update_repo()
        tag = self.tag_set.filter(name__startswith='v').last()  # TODO: implement SQL ordering for semver
        if tag is not None:
            self.version = tag.name[1:]
        robotpkg = self.robotpkg_set.order_by('-updated').first()
        branch = self.branch_set.order_by('-updated').first()
        branch_updated = branch is not None and branch.updated is not None
        robotpkg_updated = robotpkg is not None and robotpkg.updated is not None
        if branch_updated or robotpkg_updated:
            if not robotpkg_updated:
                self.updated = branch.updated
            elif not branch_updated:
                self.updated = robotpkg.updated
            else:
                self.updated = max(branch.updated, robotpkg.updated)
        self.ci_jobs()
        self.checkout()
        self.cmake()
        self.ros()
        self.save()

    def commits_since(self):
        try:
            commits = self.git().git.rev_list(f'v{self.version}..{self.main_branch()}')
            return len(commits.split('\n')) if commits else 0
        except git.exc.GitCommandError:
            pass

    def open_issues(self):
        return query_sum(self.repo_set, 'open_issues')

    def open_pr(self):
        return query_sum(self.repo_set, 'open_pr')

    def gitlabciyml(self):
        return get_template('rainboard/gitlab-ci.yml').render({'project': self})

    def contributors(self, update=False):
        if update:
            for guy in self.git().git.shortlog('-nse').split('\n'):
                name, mail = guy[7:-1].split(' <')
                contributor = get_contributor(name, mail)
                contributor.projects.add(self)
                contributor.save()
        return self.contributor_set.all()

    def registry(self):
        return settings.PUBLIC_REGISTRY if self.public else settings.PRIVATE_REGISTRY

    def doc_coverage_image(self):
        images = Image.objects.filter(robotpkg__project=self, py3=False, target__main=True)
        return images.order_by(Length('robotpkg__name').desc()).first()

    def print_deps(self):
        return mark_safe(', '.join(d.library.get_link() for d in self.dependencies.all()))

    def print_rdeps(self):
        return mark_safe(', '.join(d.project.get_link() for d in self.rdeps.all()))

    def ordered_robotpkg(self):
        return self.robotpkg_set.order_by('name')

    def url_travis(self):
        return f'https://travis-ci.org/{self.main_namespace.slug}/{self.slug}'

    def url_gitlab(self):
        return f'https://gitlab.laas.fr/{self.main_namespace.slug}/{self.slug}'

    def badge(self, link, img, alt):
        return mark_safe(f'<a href="{link}"><img src="{img}" alt="{alt}" /></a> ')

    def badge_travis(self):
        return self.badge(self.url_travis(), f'{self.url_travis()}.svg?branch=master', 'Building Status')

    def badge_gitlab(self):
        return self.badge(self.url_gitlab(), f'{self.url_gitlab()}/badges/master/pipeline.svg', 'Pipeline Status')

    def badge_coverage(self):
        return self.badge(f'{DOC_URL}/{self.main_namespace.slug}/{self.slug}/master/coverage',
                          f'{self.url_gitlab()}/badges/master/coverage.svg?job=doc-coverage"', 'Coverage Report')

    def badges(self):
        travis = self.badge_travis() if self.public else mark_safe('')
        return travis + self.badge_gitlab() + self.badge_coverage()

    def cron(self):
        """ generate a cron-style interval description to run CI monthly on master """
        hour, day = (self.pk // 30) % 24, self.pk % 30 + 1
        return f'0 {hour} {day} * *'

    def pipeline_schedules(self):
        """ provides a link to gitlab's CI schedules page showing then cron rule to use with this project """
        repo = self.repo_set.filter(forge__source=SOURCES.gitlab, namespace__group=True)
        if repo.exists():
            link = repo.first().url + '/pipeline_schedules'
            return mark_safe(f'<a href="{link}">{self.cron()}</a>')


class Repo(TimeStampedModel):
    name = models.CharField(max_length=200)
    slug = AutoSlugField(populate_from='name', slugify=slugify_with_dots)
    forge = models.ForeignKey(Forge, on_delete=models.CASCADE)
    namespace = models.ForeignKey(Namespace, on_delete=models.CASCADE)
    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    license = models.ForeignKey(License, on_delete=models.SET_NULL, blank=True, null=True)
    homepage = models.URLField(max_length=200, blank=True, null=True)
    url = models.URLField(max_length=200, blank=True, null=True)
    default_branch = models.CharField(max_length=50)
    open_issues = models.PositiveSmallIntegerField(blank=True, null=True)
    open_pr = models.PositiveSmallIntegerField(blank=True, null=True)
    repo_id = models.PositiveIntegerField()
    forked_from = models.PositiveIntegerField(blank=True, null=True)
    clone_url = models.URLField(max_length=200)
    travis_id = models.PositiveIntegerField(blank=True, null=True)
    description = models.TextField(blank=True, null=True)
    archived = models.BooleanField(default=False)

    def __str__(self):
        return self.name

    def api_url(self):
        api_url = self.forge.api_url()
        return {
            SOURCES.github: f'{api_url}/repos/{self.namespace.slug}/{self.slug}',
            SOURCES.redmine: f'{api_url}/projects/{self.repo_id}.json',
            SOURCES.gitlab: f'{api_url}/projects/{self.repo_id}',
        }[self.forge.source]

    def api_req(self, url='', name=None, page=1):
        logger.debug(f'requesting api {self.forge} {self.namespace} {self} {url}, page {page}')
        try:
            return requests.get(self.api_url() + url, {'page': page},
                                verify=self.forge.verify,
                                headers=self.forge.headers())
        except requests.exceptions.ConnectionError:
            logger.error(f'requesting api {self.forge} {self.namespace} {self} {url}, page {page} - SECOND TRY')
            return requests.get(self.api_url() + url, {'page': page},
                                verify=self.forge.verify,
                                headers=self.forge.headers())

    def api_data(self, url=''):
        req = self.api_req(url)
        return req.json() if req.status_code == 200 else []  # TODO

    def api_list(self, url='', name=None):
        page = 1
        while page:
            req = self.api_req(url, name, page)
            if req.status_code != 200:
                return []  # TODO
            data = req.json()
            if name is not None:
                if name in data:
                    data = data[name]
                else:
                    return []  # TODO
            yield from data
            page = api_next(self.forge.source, req)

    def api_update(self):
        data = self.api_data()
        if data:
            if data['archived']:
                if self.project.main_repo() == self:
                    self.project.archived = True
                    self.project.save()
                self.archived = True
                self.save()
            else:
                return getattr(self, f'api_update_{self.forge.get_source_display()}')(data)

    def api_update_gitlab(self, data):
        update_gitlab(self.forge, data)

    def api_update_github(self, data):
        update_github(self.forge, self.namespace, data)

    def get_clone_url(self):
        if self.forge.source == SOURCES.gitlab:
            return self.clone_url.replace('://', f'://gitlab-ci-token:{self.forge.token}@')
        if self.forge.source == SOURCES.github:
            return self.clone_url.replace('://', f'://{settings.GITHUB_USER}:{self.forge.token}@')
        return self.clone_url

    def git_remote(self):
        return f'{self.forge.slug}/{self.namespace.slug}'

    def git(self):
        git_repo = self.project.git()
        remote = self.git_remote()
        try:
            return git_repo.remote(remote)
        except ValueError:
            logger.info(f'Creating remote {remote}')
            return git_repo.create_remote(remote, self.get_clone_url())

    def fetch(self):
        git_repo = self.git()
        logger.debug(f'fetching {self.forge} / {self.namespace} / {self.project}')
        try:
            git_repo.fetch()
        except git.exc.GitCommandError:
            logger.warning(f'fetching {self.forge} / {self.namespace} / {self.project} - SECOND TRY')
            try:
                git_repo.fetch()
            except git.exc.GitCommandError:
                return False
        return True

    def main_branch(self):
        return self.project.branch_set.get(name=f'{self.git_remote()}/{self.default_branch}')

    def ahead(self):
        main_branch = self.main_branch()
        return main_branch.ahead if main_branch is not None else 0

    def behind(self):
        main_branch = self.main_branch()
        return main_branch.behind if main_branch is not None else 0

    def get_builds(self):
        return getattr(self, f'get_builds_{self.forge.get_source_display()}')()

    def get_builds_gitlab(self):
        for pipeline in self.api_list('/pipelines'):
            pid, ref = pipeline['id'], pipeline['ref']
            if self.project.tag_set.filter(name=ref).exists():
                continue
            data = self.api_data(f'/pipelines/{pid}')
            branch_name = f'{self.forge.slug}/{self.namespace.slug}/{ref}'
            branch, created = Branch.objects.get_or_create(name=branch_name, project=self.project, repo=self)
            if created:
                branch.update()
            ci_build, created = CIBuild.objects.get_or_create(repo=self,
                                                              build_id=pid,
                                                              defaults={
                                                                  'passed': GITLAB_STATUS[pipeline['status']],
                                                                  'started': parse_datetime(data['created_at']),
                                                                  'branch': branch,
                                                              })
            if not created and ci_build.passed != GITLAB_STATUS[pipeline['status']]:
                ci_build.passed = GITLAB_STATUS[pipeline['status']]
                ci_build.save()

    def get_jobs_gitlab(self):
        for data in self.api_list('/jobs'):
            branch_name = f'{self.forge.slug}/{self.namespace.slug}/{data["ref"]}'
            branch, created = Branch.objects.get_or_create(name=branch_name, project=self.project, repo=self)
            if created:
                branch.update()
            ci_job, created = CIJob.objects.get_or_create(repo=self,
                                                          job_id=data['id'],
                                                          defaults={
                                                              'passed': GITLAB_STATUS[data['status']],
                                                              'started': parse_datetime(data['created_at']),
                                                              'branch': branch,
                                                          })
            if not created and ci_job.passed != GITLAB_STATUS[data['status']]:
                ci_job.passed = GITLAB_STATUS[data['status']]
                ci_job.save()
            if self == self.project.main_repo() and data['name'].startswith('robotpkg-'):
                py3 = '-py3' in data['name']
                debug = '-debug' in data['name']
                target = next(target for target in Target.objects.all() if target.name in data['name']).name
                robotpkg = data['name'][9:-(2 + len(target) + (5 if debug else 7) + (3 if py3 else 0))]  # shame.
                images = Image.objects.filter(robotpkg__name=robotpkg, target__name=target, debug=debug, py3=py3)
                if not images.exists():
                    continue
                image = images.first()
                if image.allow_failure and GITLAB_STATUS[data['status']]:
                    image.allow_failure = False
                    image.save()
                    print('  success', data['web_url'])

    def get_builds_github(self):
        if self.travis_id is not None:
            travis = Forge.objects.get(source=SOURCES.travis)
            for build in travis.api_list(f'/repo/{self.travis_id}/builds', name='builds'):
                if build['branch'] is None or self.project.tag_set.filter(name=build['branch']['name']).exists():
                    continue
                branch_name = f'{self.forge.slug}/{self.namespace.slug}/{build["branch"]["name"]}'
                branch, created = Branch.objects.get_or_create(name=branch_name, project=self.project, repo=self)
                if created:
                    branch.update()
                started = build['started_at'] if build['started_at'] is not None else build['finished_at']
                CIBuild.objects.get_or_create(repo=self,
                                              build_id=build['id'],
                                              defaults={
                                                  'passed': TRAVIS_STATE[build['state']],
                                                  'started': parse_datetime(started),
                                                  'branch': branch,
                                              })

    def update(self, pull=True):
        ok = True
        if self.project.main_namespace is None:
            return
        self.project.update_tags()
        if pull:
            ok = self.fetch()
        if ok:
            self.api_update()
            self.get_builds()
        else:
            logger.error(f'fetching {self.forge} / {self.namespace} / {self.project} - NOT FOUND - DELETING')
            logger.error(str(self.delete()))


class Commit(NamedModel, TimeStampedModel):
    project = models.ForeignKey(Project, on_delete=models.CASCADE)

    class Meta:
        unique_together = ('project', 'name')


class Branch(TimeStampedModel):
    name = models.CharField(max_length=200)
    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    ahead = models.PositiveSmallIntegerField(blank=True, null=True)
    behind = models.PositiveSmallIntegerField(blank=True, null=True)
    updated = models.DateTimeField(blank=True, null=True)
    repo = models.ForeignKey(Repo, on_delete=models.CASCADE)
    deleted = models.BooleanField(default=False)
    keep_doc = models.BooleanField(default=False)

    def __str__(self):
        return self.name

    class Meta:
        unique_together = ('project', 'name', 'repo')

    def get_ahead(self, branch='master'):
        commits = self.project.git().git.rev_list(f'{branch}..{self}')
        return len(commits.split('\n')) if commits else 0

    def get_behind(self, branch='master'):
        commits = self.project.git().git.rev_list(f'{self}..{branch}')
        return len(commits.split('\n')) if commits else 0

    def git(self):
        git_repo = self.project.git()
        if self.name not in git_repo.branches:
            remote = self.repo.git()
            _, _, branch = self.name.split('/', maxsplit=2)
            git_repo.create_head(self.name, remote.refs[branch]).set_tracking_branch(remote.refs[branch])
        return git_repo.branches[self.name]

    def update(self, pull=True):
        if self.deleted:
            return
        try:
            if pull:
                self.repo.fetch()
                if self.repo != self.project.main_repo():
                    self.project.main_repo().fetch()
            try:
                main_branch = self.project.main_branch()
                self.ahead = self.get_ahead(main_branch)
                self.behind = self.get_behind(main_branch)
            except Branch.DoesNotExist:
                pass
            self.updated = self.git().commit.authored_datetime
        except (git.exc.GitCommandError, IndexError):
            self.deleted = True
        self.save()

    def ci(self):
        build = self.cibuild_set.last()
        if build is None:
            return ''
        status = {True: '✓', False: '✗', None: '?'}[build.passed]
        return mark_safe(f'<a href="{build.url()}">{status}</a>')

    def forge(self):
        return self.repo.forge

    def namespace(self):
        return self.repo.namespace


class TargetQuerySet(models.QuerySet):
    def active(self):
        return self.filter(active=True)

    def main(self):
        return self.get(main=True)


class Target(NamedModel):
    active = models.BooleanField(default=True)
    main = models.BooleanField(default=False)

    objects = TargetQuerySet.as_manager()


# class Test(TimeStampedModel):
#     project = models.ForeignKey(Project, on_delete=models.CASCADE)
#     branch = models.ForeignKey(Branch, on_delete=models.CASCADE)
#     commit = models.ForeignKey(Commit, on_delete=models.CASCADE)
#     target = models.ForeignKey(Target, on_delete=models.CASCADE)
#     passed = models.BooleanField(default=False)
#     TODO: travis vs gitlab-ci ?
#     TODO: deploy binary, doc, coverage, lint

# class SystemDependency(NamedModel):
#     project = models.ForeignKey(Project, on_delete=models.CASCADE)
#     target = models.ForeignKey(Target, on_delete=models.CASCADE)


class Robotpkg(NamedModel):
    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    category = models.CharField(max_length=50)

    pkgbase = models.CharField(max_length=50, default='')
    pkgversion = models.CharField(max_length=50, default='')
    master_sites = models.CharField(max_length=200, default='')
    master_repository = models.CharField(max_length=200, default='')
    maintainer = models.CharField(max_length=200, default='')
    comment = models.TextField()
    homepage = models.URLField(max_length=200, blank=True, null=True)

    license = models.ForeignKey(License, on_delete=models.SET_NULL, blank=True, null=True)
    public = models.BooleanField(default=True)
    description = models.TextField(blank=True, null=True)
    updated = models.DateTimeField(blank=True, null=True)

    same_py = models.BooleanField(default=True)

    def main_page(self):
        if self.category != 'wip':
            return f'{RPKG_URL}/robotpkg/{self.category}/{self.name}'

    def build_page(self):
        path = '-wip/wip' if self.category == 'wip' else f'/{self.category}'
        return f'{RPKG_URL}/rbulk/robotpkg{path}/{self.name}'

    def update_images(self):
        py3s = [False, True] if self.name.startswith('py-') else [False]
        debugs = [False, True]
        for target in Target.objects.active():
            for py3 in py3s:
                for debug in debugs:
                    Image.objects.get_or_create(robotpkg=self, target=target, py3=py3, debug=debug)[0].update()

    def update(self, pull=True):
        path = settings.RAINBOARD_RPKG
        repo = git.Repo(str(path / 'wip' / '.git' if self.category == 'wip' else path / '.git'))
        if pull:
            repo.remotes.origin.pull()

        cwd = path / self.category / self.name
        if not cwd.is_dir():
            logger.warning(f'deleted {self}: {self.delete()}')
            return
        for field in RPKG_FIELDS:
            cmd = ['make', 'show-var', f'VARNAME={field}']
            self.__dict__[field.lower()] = check_output(cmd, cwd=cwd).decode().strip()

        repo_path = self.name if self.category == 'wip' else f'{self.category}/{self.name}'
        last_commit = next(repo.iter_commits(paths=repo_path, max_count=1))
        self.updated = last_commit.authored_datetime

        license = check_output(['make', 'show-var', f'VARNAME=LICENSE'], cwd=cwd).decode().strip()
        if license in RPKG_LICENSES:
            self.license = License.objects.get(spdx_id=RPKG_LICENSES[license])
        else:
            logger.warning(f'Unknown robotpkg license: {license}')
        self.public = not bool(check_output(['make', 'show-var', f'VARNAME=RESTRICTED'], cwd=cwd).decode().strip())
        with (cwd / 'DESCR').open() as f:
            self.description = f.read().strip()

        self.update_images()
        self.save()

    def valid_images(self):
        return self.image_set.filter(created__isnull=False, target__active=True).order_by('target__name')

    def without_py(self):
        if 'py-' in self.name and self.same_py:
            return Robotpkg.objects.filter(name=self.name.replace('py-', '')).first()


# class RobotpkgBuild(TimeStampedModel):
#     robotpkg = models.ForeignKey(Robotpkg, on_delete=models.CASCADE)
#     target = models.ForeignKey(Target, on_delete=models.CASCADE)
#     passed = models.BooleanField(default=False)


class Image(models.Model):
    robotpkg = models.ForeignKey(Robotpkg, on_delete=models.CASCADE)
    target = models.ForeignKey(Target, on_delete=models.CASCADE)
    created = models.DateTimeField(blank=True, null=True)
    image = models.CharField(max_length=12, blank=True, null=True)
    py3 = models.BooleanField(default=False)
    debug = models.BooleanField(default=False)
    allow_failure = models.BooleanField(default=False)

    class Meta:
        unique_together = ('robotpkg', 'target', 'py3', 'debug')

    def __str__(self):
        py = '-py3' if self.py3 else ''
        return f'{self.robotpkg}{py}:{self.target}'

    def get_build_args(self):
        ret = {
            'TARGET': self.target,
            'ROBOTPKG': self.robotpkg,
            'CATEGORY': self.robotpkg.category,
            'REGISTRY': self.robotpkg.project.registry(),
        }
        if not self.robotpkg.project.public:
            ret['IMAGE'] = 'robotpkg-jrl-py3' if self.py3 else 'robotpkg-jrl'
        elif self.py3:
            ret['IMAGE'] = 'robotpkg-py3'
        return ret

    def get_image_name(self):
        project = self.robotpkg.project
        return f'{project.registry()}/{project.main_namespace.slug}/{project.slug}/{self}'.lower()

    def get_image_url(self):
        project = self.robotpkg.project
        manifest = str(self).replace(':', '/manifests/')
        return f'https://{project.registry()}/v2/{project.main_namespace.slug}/{project.slug}/{manifest}'

    def get_job_name(self):
        mode = 'debug' if self.debug else 'release'
        return f'robotpkg-{self}-{mode}'.replace(':', '-')

    def build(self):
        args = self.get_build_args()
        build_args = sum((['--build-arg', f'{key}={value}'] for key, value in args.items()), list())
        return ['docker', 'build', '-t', self.get_image_name()] + build_args + ['.']

    def pull(self):
        return ['docker', 'pull', self.get_image_name()]

    def push(self):
        return ['docker', 'push', self.get_image_name()]

    def update(self, pull=False):
        headers = {}
        if not self.robotpkg.project.public:
            image_name = self.get_image_name().split('/', maxsplit=1)[1].split(':')[0]
            token = requests.get(f'{self.robotpkg.project.main_forge.url}/jwt/auth', {
                'client_id': 'docker',
                'offline_token': True,
                'service': 'container_registry',
                'scope': f'repository:{image_name}:push,pull'
            },
                                 auth=('gsaurel', self.robotpkg.project.main_forge.token)).json()['token']
            headers['Authorization'] = f'Bearer {token}'
        r = requests.get(self.get_image_url(), headers=headers)
        if r.status_code == 200:
            self.image = r.json()['fsLayers'][0]['blobSum'].split(':')[1][:12]
            self.created = parse_datetime(json.loads(r.json()['history'][0]['v1Compatibility'])['created'])
            self.save()
        if not self.allow_failure and self.created and (timezone.now() - self.created).days > 7:
            self.allow_failure = True
            self.save()


class CIBuild(models.Model):
    repo = models.ForeignKey(Repo, on_delete=models.CASCADE)
    passed = models.NullBooleanField()
    build_id = models.PositiveIntegerField()
    started = models.DateTimeField()
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE)

    class Meta:
        ordering = ('-started', )

    def url(self):
        if self.repo.forge.source == SOURCES.github:
            return f'https://travis-ci.org/{self.repo.namespace.slug}/{self.repo.slug}/builds/{self.build_id}'
        if self.repo.forge.source == SOURCES.gitlab:
            return f'{self.repo.forge.url}/{self.repo.namespace.slug}/{self.repo.slug}/pipelines/{self.build_id}'


class CIJob(models.Model):
    repo = models.ForeignKey(Repo, on_delete=models.CASCADE)
    passed = models.NullBooleanField()
    job_id = models.PositiveIntegerField()
    started = models.DateTimeField()
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE)

    class Meta:
        ordering = ('-started', )


class Tag(models.Model):
    name = models.CharField(max_length=200)
    slug = AutoSlugField(populate_from='name', slugify=slugify_with_dots)
    project = models.ForeignKey(Project, on_delete=models.CASCADE)

    class Meta:
        ordering = ('name', )
        unique_together = ('name', 'project')

    def __str__(self):
        return f'{self.project} {self.name}'


class GepettistQuerySet(models.QuerySet):
    def gepettist(self):
        return self.filter(projects__from_gepetto=True, projects__archived=False)


class Contributor(models.Model):
    projects = models.ManyToManyField(Project)
    agreement_signed = models.BooleanField(default=False)

    objects = GepettistQuerySet.as_manager()

    def __str__(self):
        name = self.contributorname_set.first()
        mail = self.contributormail_set.first()
        return f'{name} <{mail}>'

    def names(self):
        return ', '.join(str(name) for name in self.contributorname_set.all())

    def mails(self):
        return ', '.join(str(mail) for mail in self.contributormail_set.filter(invalid=False))

    def contributed(self):
        return ', '.join(str(project) for project in self.projects.filter(from_gepetto=True, archived=False))


class ContributorName(models.Model):
    contributor = models.ForeignKey(Contributor, on_delete=models.CASCADE, blank=True, null=True)
    name = models.CharField(max_length=200, unique=True)

    def __str__(self):
        return self.name


class ContributorMail(models.Model):
    contributor = models.ForeignKey(Contributor, on_delete=models.CASCADE, blank=True, null=True)
    mail = models.EmailField(unique=True)
    invalid = models.BooleanField(default=False)

    def __str__(self):
        return self.mail


class Dependency(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='dependencies')
    library = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='rdeps')
    robotpkg = models.BooleanField(default=False)  # TODO NYI
    cmake = models.BooleanField(default=False)
    ros = models.BooleanField(default=False)
    mandatory = models.BooleanField(default=True)

    class Meta:
        verbose_name_plural = 'dependencies'
        unique_together = ('project', 'library')

    def __str__(self):
        return f'{self.project} depends on {self.library}: {self.robotpkg:d} {self.cmake:d}'


def get_default_forge(project):
    for forge in Forge.objects.order_by('source'):
        if project.repo_set.filter(forge=forge).exists():
            logger.info(f'default forge for {project} set to {forge}')
            project.main_forge = forge
            project.save()
            return forge
    else:
        logger.error(f'NO DEFAULT FORGE for {project}')


def update_gitlab(forge, data):
    if data['archived']:
        return
    if 'default_branch' not in data or data['default_branch'] is None:
        return
    logger.info(f'update {data["name"]} from {forge}')
    public = data['visibility'] not in ['private', 'internal']
    project, created = Project.objects.get_or_create(name=valid_name(data['name']),
                                                     defaults={
                                                         'main_forge': forge,
                                                         'public': public
                                                     })
    namespace, _ = Namespace.objects.get_or_create(slug=data['namespace']['path'],
                                                   defaults={'name': data['namespace']['name']})
    repo, _ = Repo.objects.get_or_create(forge=forge,
                                         namespace=namespace,
                                         project=project,
                                         defaults={
                                             'repo_id': data['id'],
                                             'name': data['name'],
                                             'url': data['web_url'],
                                             'default_branch': data['default_branch'],
                                             'clone_url': data['http_url_to_repo']
                                         })
    repo.name = data['name']
    repo.slug = data['path']
    repo.url = data['web_url']
    repo.repo_id = data['id']
    repo.clone_url = data['http_url_to_repo']
    if 'open_issues_count' in data:
        repo.open_issues = data['open_issues_count']
    repo.default_branch = data['default_branch']
    repo.description = data['description']
    # TODO license (https://gitlab.com/gitlab-org/gitlab-ce/issues/28267), open_pr
    if 'forked_from_project' in data and data['forked_from_project'] is not None:
        repo.forked_from = data['forked_from_project']['id']
    elif created or project.main_namespace is None:
        project.main_namespace = namespace
        project.save()
    repo.save()


def update_github(forge, namespace, data):
    if data['archived']:
        return
    logger.info(f'update {data["name"]} from {forge}')
    project, _ = Project.objects.get_or_create(name=valid_name(data['name']),
                                               defaults={
                                                   'homepage': data['homepage'],
                                                   'main_namespace': namespace,
                                                   'main_forge': forge
                                               })
    repo, _ = Repo.objects.get_or_create(forge=forge,
                                         namespace=namespace,
                                         project=project,
                                         defaults={
                                             'repo_id': data['id'],
                                             'name': data['name'],
                                             'clone_url': data['clone_url']
                                         })
    repo.homepage = data['homepage']
    repo.url = data['html_url']
    repo.repo_id = data['id']
    repo.default_branch = data['default_branch']
    repo.open_issues = data['open_issues']
    repo.description = data['description']

    repo_data = repo.api_data()
    if repo_data and 'license' in repo_data and repo_data['license']:
        if 'spdx_id' in repo_data['license'] and repo_data['license']['spdx_id']:
            if repo_data['license']['spdx_id'] != 'NOASSERTION':
                try:
                    license = License.objects.get(spdx_id=repo_data['license']['spdx_id'])
                except License.DoesNotExist:
                    raise ValueError('No License with spdx_id=' + repo_data['license']['spdx_id'])
                repo.license = license
                if not project.license:
                    project.license = license
        if 'source' in repo_data:
            repo.forked_from = repo_data['source']['id']
    if repo_data:
        repo.open_issues = repo_data['open_issues_count']
    repo.clone_url = data['clone_url']
    repo.open_pr = len(list(repo.api_list('/pulls')))
    repo.save()
    project.save()


def update_travis(namespace, data):
    project = Project.objects.filter(name=valid_name(data['name'])).first()
    if project is None:
        return
    forge = Forge.objects.get(source=SOURCES.github)
    repo, created = Repo.objects.get_or_create(forge=forge,
                                               namespace=namespace,
                                               project=project,
                                               defaults={
                                                   'name': data['name'],
                                                   'repo_id': 0,
                                                   'travis_id': data['id']
                                               })
    if created:
        repo.api_update()
    else:
        repo.travis_id = data['id']
        repo.save()


def merge_contributors(*contributors):
    logger.warning(f'merging {contributors}')
    ids = [contributor.id for contributor in contributors]
    main = min(ids)
    for model in (ContributorName, ContributorMail):
        for instance in model.objects.filter(contributor_id__in=ids):
            instance.contributor_id = main
            instance.save()
    Contributor.objects.filter(id__in=ids).exclude(id=main).delete()
    return Contributor.objects.get(id=main)


def get_contributor(name, mail):
    cname, name_created = ContributorName.objects.get_or_create(name=name)
    cmail, mail_created = ContributorMail.objects.get_or_create(mail=mail, defaults={'invalid': invalid_mail(mail)})
    if name_created or mail_created:
        if name_created and mail_created:
            contributor = Contributor.objects.create()
            cname.contributor = contributor
            cmail.contributor = contributor
            cname.save()
            cmail.save()
        if mail_created:
            contributor = cname.contributor
            cmail.contributor = contributor
            cmail.save()
        if name_created:
            contributor = cmail.contributor
            cname.contributor = cmail.contributor
            cname.save()
    elif cname.contributor == cmail.contributor or invalid_mail(mail):
        contributor = cname.contributor
    elif cname.contributor is None and cmail.contributor is not None:
        contributor = cmail.contributor
        cname.contributor = contributor
        cname.save()
    elif cmail.contributor is None:
        contributor = cname.contributor
        cmail.contributor = contributor
        cmail.save()
    else:
        contributor = merge_contributors(cname.contributor, cmail.contributor)
    return contributor


def unvalid_projects():
    return Project.objects.filter(Q(name__contains='_') | Q(name__contains='-') | Q(slug__endswith='-2'))


def fix_unvalid_projects():
    for prj in unvalid_projects():
        if prj.slug.endswith('-2'):
            prj.slug = prj.slug[:-2]
        prj.name = valid_name(prj.name)
        prj.save()


def to_release_in_robotpkg():
    for robotpkg in Robotpkg.objects.all():
        if robotpkg.pkgversion.split('r')[0] != robotpkg.project.version:
            if 'alpha' not in str(robotpkg.project.version):
                print(robotpkg, robotpkg.pkgversion, robotpkg.project.version)


def ordered_projects():
    """ helper for gepetto/buildfarm/generate_all.py """
    fields = 'category', 'name', 'project__main_namespace__slug'
    bad_ones = Q(from_gepetto=False) | Q(robotpkg__isnull=True) | Q(archived=True)
    library_bad_ones = Q(library__from_gepetto=False) | Q(library__robotpkg__isnull=True)

    main = Project.objects.exclude(bad_ones)
    ret = main.all().exclude(dependencies__isnull=False)
    rest = main.all().exclude(id__in=ret)
    lst = sorted(list(Robotpkg.objects.filter(project__in=ret).values_list(*fields)))
    print(ret.count(), rest.count())

    while rest.exists():
        new_ret = []
        for prj in rest:
            if all(d.library in ret for d in prj.dependencies.exclude(library_bad_ones)):
                new_ret.append(prj)
        lst += sorted(list(Robotpkg.objects.filter(project__in=new_ret).values_list(*fields)))
        ret = Project.objects.filter(Q(id__in=ret) | Q(id__in=[p.id for p in new_ret]))
        rest = rest.exclude(id__in=[p.id for p in new_ret])
        print(ret.count(), rest.count())

    # Ensure that py-XX is after XX
    switch = []
    for i, (cat, pkg, ns) in enumerate(lst):
        main = (cat, pkg[3:], ns)
        if pkg.startswith('py-') and main in lst and i < lst.index(main):
            switch.append((i, lst.index(main)))
    for old, new in switch:
        lst[old], lst[new] = lst[new], lst[old]

    def get_deps(cat, pkg, ns, lst):
        with (settings.RAINBOARD_RPKG / cat / pkg / 'Makefile').open() as file_handle:
            cont = file_handle.read()
        deps = [dep_pkg for dep_cat, dep_pkg, _ in lst if f'\ninclude ../../{dep_cat}/{dep_pkg}/depend.mk\n' in cont]
        if pkg.startswith('py-') and (cat, pkg[3:], ns) in lst:
            deps.append(pkg[3:])
        return list(set(deps))

    return [[cat, pkg, ns, get_deps(cat, pkg, ns, lst)] for cat, pkg, ns in lst]
