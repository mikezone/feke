from argparse import RawTextHelpFormatter
# from django.core.management.templates import TemplateCommand
from django.core.management.utils import get_random_secret_key
# from feke.djangoext.commands import BaseCommand, TemplateCommand
from feke.core.management.base import BaseCommand, TemplateCommand

class Command(TemplateCommand):
    help = (
        "Description:\n"
        "Create a new Hexo folder at the specified path or the current directory."
    )
    missing_args_message = "You must provide a project name."

    def create_parser(self, *args, **kwargs):
        parser = super(Command, self).create_parser(*args, **kwargs)
        parser.formatter_class = RawTextHelpFormatter
        return parser

    # def add_arguments(self, parser):
    #     # destination  Folder path. Initialize in current folder if not specified
    #     parser.add_argument(
    #         '-d', '--destination',
    #         action='store',
    #         dest='destination',
    #         # default=1,
    #         # type=int, choices=[0, 1, 2, 3],
    #         help='Folder path. Initialize in current folder if not specified',
    #     )

    # def handle(self, **options):
        # project_name = options.pop('name')
        # target = options.pop('directory')
        # print(options)

        # Create a random SECRET_KEY to put it in the main settings.

        # super().handle('project', project_name, target, **options)
    def handle(self, **options):
        project_name = options.pop('name')
        target = options.pop('directory')

        # Create a random SECRET_KEY to put it in the main settings.
        options['secret_key'] = get_random_secret_key()

        super().handle('project', project_name, target, **options)
