"""Views for dashboard_apps."""
import hmac
from hashlib import sha1
from ipaddress import ip_address, ip_network
from json import loads
from pprint import pprint

from django.conf import settings
from django.http import HttpRequest
from django.http.response import (HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, HttpResponseRedirect,
                                  HttpResponseServerError)
from django.shortcuts import get_object_or_404, reverse
from django.utils.encoding import force_bytes
from django.views.decorators.csrf import csrf_exempt

import requests
from autoslug.utils import slugify

from dashboard.middleware import ip_laas
from rainboard.models import Forge, Namespace, Project

from . import models


def check_suite(request: HttpRequest, rep: str) -> HttpResponse:
    """Manage Github's check suites."""
    data = loads(request.body.decode())
    models.GithubCheckSuite.objects.get_or_create(id=data['check_suite']['id'])
    return HttpResponse(rep)


def pull_request(request: HttpRequest, rep: str) -> HttpResponse:
    """Manage Github's Pull Requests."""
    data = loads(request.body.decode())
    namespace = get_object_or_404(Namespace, slug=slugify(data['repository']['owner']['login']))
    project = get_object_or_404(Project, main_namespace=namespace, slug=slugify(data['repository']['name']))
    git_repo = project.git()
    remote_s = f'github/{data["pull_request"]["head"]["repo"]["owner"]["login"]}'
    if remote_s not in git_repo.remotes:
        git_repo.create_remote(remote_s, data["pull_request"]["head"]["repo"]["clone_url"])
    remote = git_repo.remotes[remote_s]
    remote.fetch()
    branch = f'pr/{data["number"]}'
    commit = data['pull_request']['head']['sha']
    if branch in git_repo.branches:
        git_repo.heads[branch] = commit
    else:
        git_repo.create_head(branch, commit=commit)
    print(f'pushing {commit} on {branch} on gitlab')
    gl_remote = git_repo.remotes[f'gitlab/{namespace.slug}']
    gl_remote.push(branch)
    return HttpResponse(rep)


def push(request: HttpRequest, rep: str) -> HttpResponse:
    """Someone pushed on github. Synchronise local repo & gitlab."""
    data = loads(request.body.decode())
    namespace = get_object_or_404(Namespace, slug=slugify(data['repository']['owner']['name']))
    project = get_object_or_404(Project, main_namespace=namespace, slug=slugify(data['repository']['name']))
    ref_s = data['ref'][11:]  # strip 'refs/heads/'
    print(f'push detected on github: {ref_s}')
    gh_remote_s = f'github/{namespace.slug}'
    gl_remote_s = f'gitlab/{namespace.slug}'
    gh_ref_s = f'{gh_remote_s}/{ref_s}'
    gl_ref_s = f'{gl_remote_s}/{ref_s}'

    git_repo = project.git()
    gh_remote = git_repo.remotes[gh_remote_s]
    gh_remote.fetch()
    gh_ref = gh_remote.refs[ref_s]
    if data['after'] == "0000000000000000000000000000000000000000":
        print("branch deleted")
        for ref in [gh_ref_s, gl_ref_s, ref_s]:
            if ref in git_repo.branches:
                git_repo.delete_head(ref, force=True)
        gitlab = Forge.objects.get(slug='gitlab')
        project_u = f'{namespace.slug}/{project.slug}'.replace('/', '%2F')
        branch_u = ref_s.replace('/', '%2F')
        url = f'/projects/{project_u}/repository/branches/{branch_u}'
        requests.delete(gitlab.api_url() + url, verify=gitlab.verify, headers=gitlab.headers())
        return HttpResponse(rep)

    if str(gh_ref.commit) != data['after']:
        fail = f'push: wrong commit: {gh_ref.commit} vs {data["after"]}'
        print(fail)
        return HttpResponseBadRequest(fail)

    if gh_ref_s in git_repo.branches:
        git_repo.branches[gh_ref_s].commit = data['after']
    else:
        git_repo.create_head(gh_ref_s, commit=data['after'])
    if ref_s in git_repo.branches:
        git_repo.branches[ref_s].commit = data['after']
    else:
        git_repo.create_head(ref_s, commit=data['after'])

    if gl_remote_s not in git_repo.remotes:
        print(f'project {project} not available on {gl_remote_s}')
        return HttpResponse(rep)

    if gl_ref_s in git_repo.branches:
        git_repo.branches[gl_ref_s].commit = data['after']
    else:
        git_repo.create_head(gl_ref_s, commit=data['after'])

    gl_remote = git_repo.remotes[gl_remote_s]
    gl_remote.fetch()
    if ref_s not in gl_remote.refs or str(gl_remote.refs[ref_s].commit) != data['after']:
        print(f'pushing {data["after"]} on {ref_s} on gitlab')
        gl_remote.push(ref_s)

    return HttpResponse(rep)


def pipeline(request: HttpRequest, rep: str) -> HttpResponse:
    """Something happened on a Gitlab pipeline. Tell Github if necessary."""
    print('pipeline')
    data = loads(request.body.decode())
    namespace = get_object_or_404(Namespace, slug=slugify(data['project']['namespace']))
    project = get_object_or_404(Project, main_namespace=namespace, slug=slugify(data['project']['name']))
    branch, commit, status = (data['object_attributes'][key] for key in ['ref', 'sha', 'status'])
    # status in ['pending', 'running', 'success', 'failed']
    print(namespace, project, branch, commit, status)
    return HttpResponse(rep)


@csrf_exempt
def webhook(request: HttpRequest) -> HttpResponse:
    """
    Process request incoming from a github webhook.

    thx https://simpleisbetterthancomplex.com/tutorial/2016/10/31/how-to-handle-github-webhooks-using-django.html
    """
    # validate ip source
    forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    networks = requests.get('https://api.github.com/meta').json()['hooks']
    if not any(ip_address(forwarded_for) in ip_network(net) for net in networks):
        print('not from github IP')
        return HttpResponseRedirect(reverse('login'))

    # validate signature
    signature = request.META.get('HTTP_X_HUB_SIGNATURE')
    if signature is None:
        print('no signature')
        return HttpResponseRedirect(reverse('login'))
    algo, signature = signature.split('=')
    if algo != 'sha1':
        print('signature not sha-1')
        return HttpResponseServerError('I only speak sha1.', status=501)

    mac = hmac.new(force_bytes(settings.GITHUB_WEBHOOK_KEY), msg=force_bytes(request.body), digestmod=sha1)
    if not hmac.compare_digest(force_bytes(mac.hexdigest()), force_bytes(signature)):
        print('wrong signature')
        return HttpResponseForbidden('wrong signature.')

    # process event
    event = request.META.get('HTTP_X_GITHUB_EVENT', 'ping')
    if event == 'ping':
        pprint(loads(request.body.decode()))
        return HttpResponse('pong')
    if event == 'push':
        return push(request, 'push event detected')
    if event == 'check_suite':
        return check_suite(request, 'check_suite event detected')
    if event == 'pull_request':
        return pull_request(request, 'check_suite event detected')

    return HttpResponseForbidden('event not found')


@csrf_exempt
def gl_webhook(request: HttpRequest) -> HttpResponse:
    # validate ip source
    if not ip_laas(request):
        print('not from LAAS IP')
        return HttpResponseRedirect(reverse('login'))

    # validate token
    token = request.META.get('HTTP_X_GITLAB_TOKEN')
    if token is None:
        print('no token')
        return HttpResponseRedirect(reverse('login'))
    if token != settings.GITLAB_WEBHOOK_KEY:
        print('wrong token')
        return HttpResponseForbidden('wrong token.')

    event = request.META.get('HTTP_X_GITLAB_EVENT', 'ping')
    if event == 'ping':
        pprint(loads(request.body.decode()))
        return HttpResponse('pong')
    if event == 'Pipeline Hook':
        return pipeline(request, 'pipeline event detected')

    return HttpResponseForbidden('event not found')
