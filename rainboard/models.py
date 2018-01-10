from django.db import models
from django.urls import reverse

from autoslug import AutoSlugField
from ndh.models import NamedModel, TimeStampedModel, Links
from ndh.utils import enum_to_choices, query_sum

import requests

from .utils import SOURCES, TARGETS


class Namespace(NamedModel):
    pass


class License(NamedModel):
    github_key = models.CharField(max_length=50)
    spdx_id = models.CharField(max_length=50)
    url = models.URLField(max_length=200)

    def __str__(self):
        return self.spdx_id or self.name


class Project(Links, NamedModel, TimeStampedModel):
    private = models.BooleanField(default=False)
    main_namespace = models.ForeignKey(Namespace, on_delete=models.SET_NULL, null=True, blank=True)
    license = models.ForeignKey(License, on_delete=models.SET_NULL, blank=True, null=True)
    homepage = models.URLField(max_length=200, blank=True, null=True)

    def get_absolute_url(self):
        return reverse('rainboard:project', kwargs={'slug': self.slug})


class Forge(Links, NamedModel):
    source = models.PositiveSmallIntegerField(choices=enum_to_choices(SOURCES))
    url = models.URLField(max_length=200)
    token = models.CharField(max_length=50, blank=True, null=True)
    verify = models.BooleanField(default=True)

    def get_absolute_url(self):
        return self.url

    def api_data(self, url=''):
        return requests.get(self.api_url() + url, verify=self.verify, headers=self.headers()).json()

    def headers(self):
        if self.source == SOURCES.github:
            return {'Authorization': f'token {self.token}', 'Accept': 'application/vnd.github.drax-preview+json'}
        if self.source == SOURCES.gitlab:
            return {'Private-Token': self.token}
        if self.source == SOURCES.redmine:
            return {'X-Redmine-API-Key': self.token}

    def api_url(self):
        if self.source == SOURCES.github:
            return 'https://api.github.com'
        if self.source == SOURCES.gitlab:
            return f'{self.url}/api/v4'
        return self.url

    def get_projects(self):  # TODO auto
        if self.source == SOURCES.github:
            return self.get_projects_github()
        if self.source == SOURCES.gitlab:
            return self.get_projects_gitlab()
        if self.source == SOURCES.redmine:
            return self.get_projects_redmine()

    def get_projects_github(self):
        for namespace in Namespace.objects.all():
            for data in self.api_data(f'/orgs/{namespace.slug}/repos'):
                if not 'name' in data:
                    continue
                project, _ = Project.objects.get_or_create(name=data['name'],
                                                           defaults={'homepage': data['homepage'],
                                                                     'main_namespace': namespace})
                repo, _ = Repo.objects.get_or_create(forge=self, namespace=namespace, project=project,
                                                     defaults={'repo_id': data['id'], 'name': data['name']})
                repo.homepage = data['homepage']
                repo.url = data['html_url']
                repo.repo_id = data['id']
                repo.default_branch = data['default_branch']
                repo.open_issues = data['open_issues']

                repo_data = repo.api_data()
                if 'license' in repo_data and repo_data['license']:
                    license_data = repo_data['license']
                    license, _ = License.objects.get_or_create(name=license_data['name'],
                                                               defaults={'github_key': license_data['key']})
                    repo.license = license
                    if not project.license:
                        project.license = license
                repo.open_pr = len(repo.api_data('/pulls'))
                repo.save()
                project.save()

    def get_projects_gitlab(self):
        def update_gitlab(data):
            project, created = Project.objects.get_or_create(name=data['name'])
            namespace, _ = Namespace.objects.get_or_create(name=data['namespace']['name'])
            repo, _ = Repo.objects.get_or_create(forge=self, namespace=namespace, project=project,
                                                 defaults={'repo_id': data['id'], 'name': data['name'],
                                                           'url': data['web_url']})
            if 'forked_from_project' in data:
                repo.forked_from = data['forked_from_project']['id']
                repo.save()
            elif created or project.main_namespace is None:
                project.main_namespace = namespace
                project.save()

        api = self.api_data('/projects')
        for data in api:
            update_gitlab(data)

        for orphan in Project.objects.filter(main_namespace=None):
            repo = orphan.repo_set.get(forge__source=SOURCES.gitlab)
            update_gitlab(self.api_data(f'/projects/{repo.forked_from}'))

    def get_projects_redmine(self):
        pass  # TODO


class Repo(TimeStampedModel):
    name = models.CharField(max_length=200)
    slug = AutoSlugField(populate_from='name')
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

    def api_url(self):
        if self.forge.source == SOURCES.github:
            return f'{self.forge.api_url()}/repos/{self.namespace.slug}/{self.slug}'
        if self.forge.source == SOURCES.redmine:
            return f'{self.forge.api_url()}/projects/{self.repo_id}.json'

    def api_data(self, url=''):
        return requests.get(self.api_url() + url, verify=self.forge.verify, headers=self.forge.headers()).json()


class Commit(NamedModel, TimeStampedModel):
    project = models.ForeignKey(Project, on_delete=models.CASCADE)


class Branch(NamedModel, TimeStampedModel):
    repo = models.ForeignKey(Repo, on_delete=models.CASCADE)
    commit = models.ForeignKey(Commit, on_delete=models.CASCADE)

    def __str__(self):
        return f'{self.repo}/{self.name}'


class Test(TimeStampedModel):
    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE)
    commit = models.ForeignKey(Commit, on_delete=models.CASCADE)
    target = models.PositiveSmallIntegerField(choices=enum_to_choices(TARGETS))
    passed = models.BooleanField(default=False)
    # TODO: travis vs gitlab-ci ?
    # TODO: deploy binary, doc, coverage, lint


class SystemDependency(NamedModel):
    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    target = models.PositiveSmallIntegerField(choices=enum_to_choices(TARGETS))


class RobotpkgDependency(NamedModel):
    project = models.ForeignKey(Project, on_delete=models.CASCADE)


class Robotpkg(NamedModel):
    project = models.OneToOneField(Project, on_delete=models.CASCADE)
    license = models.ForeignKey(License, on_delete=models.SET_NULL, blank=True, null=True)
    homepage = models.URLField(max_length=200, blank=True, null=True)


class RobotpkgBuild(TimeStampedModel):
    robotpkg = models.ForeignKey(Robotpkg, on_delete=models.CASCADE)
    target = models.PositiveSmallIntegerField(choices=enum_to_choices(TARGETS))
    passed = models.BooleanField(default=False)


# TODO: later
# class Dockerfile(NamedModel, TimeStampedModel):
    # project = models.ForeignKey(Project, on_delete=models.CASCADE)
    # target = models.PositiveSmallIntegerField(choices=enum_to_choices(TARGETS))
