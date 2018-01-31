from django.shortcuts import render
from django.views.generic import DetailView, CreateView

from django_filters.views import FilterView
from django_tables2 import RequestConfig
from django_tables2.views import SingleTableMixin, SingleTableView

from . import models
from . import tables
from .filters import ProjectFilter


class ArticlesView(SingleTableView):
    model = models.Article
    table_class = tables.ArticleTable


class ArticleCreateView(CreateView):
    model = models.Article
    fields = ('name', 'authors', 'year', 'url', 'pdf')


class ForgesView(SingleTableView):
    model = models.Forge
    table_class = tables.ForgeTable


class NamespacesView(SingleTableView):
    model = models.Namespace
    table_class = tables.NamespaceTable


class ProjectsView(SingleTableMixin, FilterView):
    model = models.Project
    table_class = tables.ProjectTable
    filterset_class = ProjectFilter


class ProjectView(DetailView):
    model = models.Project

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        repos = tables.RepoTable(self.object.repo_set.all())
        RequestConfig(self.request).configure(repos)
        ctx['repos'] = repos
        return ctx


class ProjectBranchesView(DetailView):
    model = models.Project
    template_name = 'rainboard/project_detail_branches.html'

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        branches = tables.BranchTable(self.object.branch_set.all())
        RequestConfig(self.request).configure(branches)
        ctx['branches'] = branches
        return ctx
