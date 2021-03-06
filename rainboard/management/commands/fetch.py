import logging

from django.core.management.base import BaseCommand
from rainboard.models import Project

logger = logging.getLogger('rainboard.management.fetch')


class Command(BaseCommand):
    help = 'Fetch all remotes'

    def handle(self, *args, **options):
        logger.info(f'Fetching all repos')
        for project in Project.objects.all():
            logger.info(f' updating branches for {project}')
            project.update_branches(main=False, pull=True)
