"""
Base classes for writing management commands (named commands which can
be executed through ``django-admin`` or ``manage.py``).
"""
import os
import sys
from argparse import ArgumentParser, HelpFormatter, RawTextHelpFormatter
from io import TextIOBase
from importlib import import_module
from django.core.management.utils import handle_extensions
from django.template import Context, Engine
import shutil
import feke
# import django
# from django.core import checks
# from django.core.exceptions import ImproperlyConfigured
# from django.core.management.color import color_style, no_style
# from django.db import DEFAULT_DB_ALIAS, connections


class CommandError(Exception):
    """
    Exception class indicating a problem while executing a management
    command.

    If this exception is raised during the execution of a management
    command, it will be caught and turned into a nicely-printed error
    message to the appropriate output stream (i.e., stderr); as a
    result, raising this exception (with a sensible description of the
    error) is the preferred way to indicate that something has gone
    wrong in the execution of a command.
    """
    pass


class SystemCheckError(CommandError):
    """
    The system check framework detected unrecoverable errors.
    """
    pass


class CommandParser(ArgumentParser):
    """
    Customized ArgumentParser class to improve some error messages and prevent
    SystemExit in several occasions, as SystemExit is unacceptable when a
    command is called programmatically.
    """
    def __init__(self, **kwargs):
        self.missing_args_message = kwargs.pop('missing_args_message', None)
        self.called_from_command_line = kwargs.pop('called_from_command_line', None)
        super().__init__(**kwargs)

    def parse_args(self, args=None, namespace=None):
        # Catch missing argument for a better error message
        if (self.missing_args_message and
                not (args or any(not arg.startswith('-') for arg in args))):
            self.error(self.missing_args_message)
        return super().parse_args(args, namespace)

    def error(self, message):
        if self.called_from_command_line:
            super().error(message)
        else:
            raise CommandError("Error: %s" % message)


def handle_default_options(options):
    """
    Include any default options that all commands should accept here
    so that ManagementUtility can handle them before searching for
    user commands.
    """
    # if options.settings:
    #     os.environ['DJANGO_SETTINGS_MODULE'] = options.settings
    # if options.pythonpath:
    #     sys.path.insert(0, options.pythonpath)


def no_translations(handle_func):
    """Decorator that forces a command to run with translations deactivated."""
    def wrapped(*args, **kwargs):
        from django.utils import translation
        saved_locale = translation.get_language()
        translation.deactivate_all()
        try:
            res = handle_func(*args, **kwargs)
        finally:
            if saved_locale is not None:
                translation.activate(saved_locale)
        return res
    return wrapped


class DjangoHelpFormatter(HelpFormatter):
    """
    Customized formatter so that command-specific arguments appear in the
    --help output before arguments common to all commands.
    """
    show_last = {
        '--version', '--verbosity', '--traceback', '--settings', '--pythonpath',
        '--no-color',
    }

    def _reordered_actions(self, actions):
        return sorted(
            actions,
            key=lambda a: set(a.option_strings) & self.show_last != set()
        )

    def add_usage(self, usage, actions, *args, **kwargs):
        super().add_usage(usage, self._reordered_actions(actions), *args, **kwargs)

    def add_arguments(self, actions):
        super().add_arguments(self._reordered_actions(actions))


class OutputWrapper(TextIOBase):
    """
    Wrapper around stdout/stderr
    """
    @property
    def style_func(self):
        return self._style_func

    @style_func.setter
    def style_func(self, style_func):
        if style_func and self.isatty():
            self._style_func = style_func
        else:
            self._style_func = lambda x: x

    def __init__(self, out, style_func=None, ending='\n'):
        self._out = out
        self.style_func = None
        self.ending = ending

    def __getattr__(self, name):
        return getattr(self._out, name)

    def isatty(self):
        return hasattr(self._out, 'isatty') and self._out.isatty()

    def write(self, msg, style_func=None, ending=None):
        ending = self.ending if ending is None else ending
        if ending and not msg.endswith(ending):
            msg += ending
        style_func = style_func or self.style_func
        self._out.write(style_func(msg))


class BaseCommand:
    """
    The base class from which all management commands ultimately
    derive.

    Use this class if you want access to all of the mechanisms which
    parse the command-line arguments and work out what code to call in
    response; if you don't need to change any of that behavior,
    consider using one of the subclasses defined in this file.

    If you are interested in overriding/customizing various aspects of
    the command-parsing and -execution behavior, the normal flow works
    as follows:

    1. ``django-admin`` or ``manage.py`` loads the command class
       and calls its ``run_from_argv()`` method.

    2. The ``run_from_argv()`` method calls ``create_parser()`` to get
       an ``ArgumentParser`` for the arguments, parses them, performs
       any environment changes requested by options like
       ``pythonpath``, and then calls the ``execute()`` method,
       passing the parsed arguments.

    3. The ``execute()`` method attempts to carry out the command by
       calling the ``handle()`` method with the parsed arguments; any
       output produced by ``handle()`` will be printed to standard
       output and, if the command is intended to produce a block of
       SQL statements, will be wrapped in ``BEGIN`` and ``COMMIT``.

    4. If ``handle()`` or ``execute()`` raised any exception (e.g.
       ``CommandError``), ``run_from_argv()`` will  instead print an error
       message to ``stderr``.

    Thus, the ``handle()`` method is typically the starting point for
    subclasses; many built-in commands and command types either place
    all of their logic in ``handle()``, or perform some additional
    parsing work in ``handle()`` and then delegate from it to more
    specialized methods as needed.

    Several attributes affect behavior at various steps along the way:

    ``help``
        A short description of the command, which will be printed in
        help messages.

    ``output_transaction``
        A boolean indicating whether the command outputs SQL
        statements; if ``True``, the output will automatically be
        wrapped with ``BEGIN;`` and ``COMMIT;``. Default value is
        ``False``.

    ``requires_migrations_checks``
        A boolean; if ``True``, the command prints a warning if the set of
        migrations on disk don't match the migrations in the database.

    ``requires_system_checks``
        A boolean; if ``True``, entire Django project will be checked for errors
        prior to executing the command. Default value is ``True``.
        To validate an individual application's models
        rather than all applications' models, call
        ``self.check(app_configs)`` from ``handle()``, where ``app_configs``
        is the list of application's configuration provided by the
        app registry.

    ``stealth_options``
        A tuple of any options the command uses which aren't defined by the
        argument parser.
    """
    # Metadata about this command.
    help = ''

    # Configuration shortcuts that alter various logic.
    _called_from_command_line = False
    output_transaction = False  # Whether to wrap the output in a "BEGIN; COMMIT;"
    requires_migrations_checks = False
    requires_system_checks = True
    # Arguments, common to all commands, which aren't defined by the argument
    # parser.
    base_stealth_options = ('skip_checks', 'stderr', 'stdout')
    # Command-specific options not defined by the argument parser.
    stealth_options = ()

    def __init__(self, stdout=None, stderr=None, no_color=False):
        self.stdout = OutputWrapper(stdout or sys.stdout)
        self.stderr = OutputWrapper(stderr or sys.stderr)
        # if no_color:
        #     self.style = no_style()
        # else:
        #     self.style = color_style()
        #     self.stderr.style_func = self.style.ERROR

    def get_version(self):
        """
        Return the Django version, which should be correct for all built-in
        Django commands. User-supplied commands can override this method to
        return their own version.
        """
        return feke.get_version()

    def create_parser(self, prog_name, subcommand):
        """
        Create and return the ``ArgumentParser`` which will be used to
        parse the arguments to this command.
        """
        parser = CommandParser(
            prog='%s %s' % (os.path.basename(prog_name), subcommand),
            description=self.help or None,
            formatter_class=RawTextHelpFormatter,
            missing_args_message=getattr(self, 'missing_args_message', None),
            called_from_command_line=getattr(self, '_called_from_command_line', None),
        )
        # parser.add_argument('--version', action='version', version=self.get_version())
        # parser.add_argument(
        #     '-v', '--verbosity', action='store', dest='verbosity', default=1,
        #     type=int, choices=[0, 1, 2, 3],
        #     help='Verbosity level; 0=minimal output, 1=normal output, 2=verbose output, 3=very verbose output',
        # )
        # parser.add_argument(
        #     '--settings',
        #     help=(
        #         'The Python path to a settings module, e.g. '
        #         '"myproject.settings.main". If this isn\'t provided, the '
        #         'DJANGO_SETTINGS_MODULE environment variable will be used.'
        #     ),
        # )
        # parser.add_argument(
        #     '--pythonpath',
        #     help='A directory to add to the Python path, e.g. "/home/djangoprojects/myproject".',
        # )
        # parser.add_argument('--traceback', action='store_true', help='Raise on CommandError exceptions')
        # parser.add_argument(
        #     '--no-color', action='store_true', dest='no_color',
        #     help="Don't colorize the command output.",
        # )
        self.add_arguments(parser)
        return parser

    def add_arguments(self, parser):
        """
        Entry point for subclassed commands to add custom arguments.
        """
        pass

    def print_help(self, prog_name, subcommand):
        """
        Print the help message for this command, derived from
        ``self.usage()``.
        """
        parser = self.create_parser(prog_name, subcommand)
        parser.print_help()

    def run_from_argv(self, argv):
        """
        Set up any environment changes requested (e.g., Python path
        and Django settings), then run this command. If the
        command raises a ``CommandError``, intercept it and print it sensibly
        to stderr. If the ``--traceback`` option is present or the raised
        ``Exception`` is not ``CommandError``, raise it.
        """
        self._called_from_command_line = True
        parser = self.create_parser(argv[0], argv[1])

        options = parser.parse_args(argv[2:])
        cmd_options = vars(options)
        # Move positional args out of options to mimic legacy optparse
        args = cmd_options.pop('args', ())
        handle_default_options(options)
        try:
            self.execute(*args, **cmd_options)
        except Exception as e:
            # if options.traceback or not isinstance(e, CommandError):
            #     raise

            # SystemCheckError takes care of its own formatting.
            if isinstance(e, SystemCheckError):
                self.stderr.write(str(e), lambda x: x)
            else:
                self.stderr.write('%s: %s' % (e.__class__.__name__, e))
            sys.exit(1)
        # finally:
            # try:
            #     connections.close_all()
            # except ImproperlyConfigured:
            #     # Ignore if connections aren't setup at this point (e.g. no
            #     # configured settings).
            #     pass

    def execute(self, *args, **options):
        """
        Try to execute this command, performing system checks if needed (as
        controlled by the ``requires_system_checks`` attribute, except if
        force-skipped).
        """
        # if options['no_color']:
            # self.style = no_style()
            # self.stderr.style_func = None
        # if options.get('stdout'):
        #     self.stdout = OutputWrapper(options['stdout'])
        # if options.get('stderr'):
        #     self.stderr = OutputWrapper(options['stderr'], self.stderr.style_func)
        #
        # if self.requires_system_checks and not options.get('skip_checks'):
        #     self.check()
        # if self.requires_migrations_checks:
        #     self.check_migrations()
        output = self.handle(*args, **options)
        if output:
            # if self.output_transaction:
                # connection = connections[options.get('database', DEFAULT_DB_ALIAS)]
                # output = '%s\n%s\n%s' % (
                #     self.style.SQL_KEYWORD(connection.ops.start_transaction_sql()),
                #     output,
                #     self.style.SQL_KEYWORD(connection.ops.end_transaction_sql()),
                # )
            self.stdout.write(output)
        return output

    # def _run_checks(self, **kwargs):
    #     return checks.run_checks(**kwargs)

    # def check(self, app_configs=None, tags=None, display_num_errors=False,
    #           include_deployment_checks=False, fail_level=checks.ERROR):
    #     """
    #     Use the system check framework to validate entire Django project.
    #     Raise CommandError for any serious message (error or critical errors).
    #     If there are only light messages (like warnings), print them to stderr
    #     and don't raise an exception.
    #     """
    #     all_issues = self._run_checks(
    #         app_configs=app_configs,
    #         tags=tags,
    #         include_deployment_checks=include_deployment_checks,
    #     )
    #
    #     header, body, footer = "", "", ""
    #     visible_issue_count = 0  # excludes silenced warnings
    #
    #     if all_issues:
    #         debugs = [e for e in all_issues if e.level < checks.INFO and not e.is_silenced()]
    #         infos = [e for e in all_issues if checks.INFO <= e.level < checks.WARNING and not e.is_silenced()]
    #         warnings = [e for e in all_issues if checks.WARNING <= e.level < checks.ERROR and not e.is_silenced()]
    #         errors = [e for e in all_issues if checks.ERROR <= e.level < checks.CRITICAL and not e.is_silenced()]
    #         criticals = [e for e in all_issues if checks.CRITICAL <= e.level and not e.is_silenced()]
    #         sorted_issues = [
    #             (criticals, 'CRITICALS'),
    #             (errors, 'ERRORS'),
    #             (warnings, 'WARNINGS'),
    #             (infos, 'INFOS'),
    #             (debugs, 'DEBUGS'),
    #         ]
    #
    #         for issues, group_name in sorted_issues:
    #             if issues:
    #                 visible_issue_count += len(issues)
    #                 formatted = (
    #                     self.style.ERROR(str(e))
    #                     if e.is_serious()
    #                     else self.style.WARNING(str(e))
    #                     for e in issues)
    #                 formatted = "\n".join(sorted(formatted))
    #                 body += '\n%s:\n%s\n' % (group_name, formatted)
    #
    #     if visible_issue_count:
    #         header = "System check identified some issues:\n"
    #
    #     if display_num_errors:
    #         if visible_issue_count:
    #             footer += '\n'
    #         footer += "System check identified %s (%s silenced)." % (
    #             "no issues" if visible_issue_count == 0 else
    #             "1 issue" if visible_issue_count == 1 else
    #             "%s issues" % visible_issue_count,
    #             len(all_issues) - visible_issue_count,
    #         )
    #
    #     if any(e.is_serious(fail_level) and not e.is_silenced() for e in all_issues):
    #         msg = self.style.ERROR("SystemCheckError: %s" % header) + body + footer
    #         raise SystemCheckError(msg)
    #     else:
    #         msg = header + body + footer
    #
    #     if msg:
    #         if visible_issue_count:
    #             self.stderr.write(msg, lambda x: x)
    #         else:
    #             self.stdout.write(msg)

    # def check_migrations(self):
    #     """
    #     Print a warning if the set of migrations on disk don't match the
    #     migrations in the database.
    #     """
    #     from django.db.migrations.executor import MigrationExecutor
    #     try:
    #         executor = MigrationExecutor(connections[DEFAULT_DB_ALIAS])
    #     except ImproperlyConfigured:
    #         # No databases are configured (or the dummy one)
    #         return
    #
    #     plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    #     if plan:
    #         apps_waiting_migration = sorted({migration.app_label for migration, backwards in plan})
    #         self.stdout.write(
    #             self.style.NOTICE(
    #                 "\nYou have %(unpplied_migration_count)s unapplied migration(s). "
    #                 "Your project may not work properly until you apply the "
    #                 "migrations for app(s): %(apps_waiting_migration)s." % {
    #                     "unpplied_migration_count": len(plan),
    #                     "apps_waiting_migration": ", ".join(apps_waiting_migration),
    #                 }
    #             )
    #         )
    #         self.stdout.write(self.style.NOTICE("Run 'python manage.py migrate' to apply them.\n"))

    def handle(self, *args, **options):
        """
        The actual logic of the command. Subclasses must implement
        this method.
        """
        raise NotImplementedError('subclasses of BaseCommand must provide a handle() method')


class AppCommand(BaseCommand):
    """
    A management command which takes one or more installed application labels
    as arguments, and does something with each of them.

    Rather than implementing ``handle()``, subclasses must implement
    ``handle_app_config()``, which will be called once for each application.
    """
    missing_args_message = "Enter at least one application label."

    def add_arguments(self, parser):
        parser.add_argument('args', metavar='app_label', nargs='+', help='One or more application label.')

    def handle(self, *app_labels, **options):
        from django.apps import apps
        try:
            app_configs = [apps.get_app_config(app_label) for app_label in app_labels]
        except (LookupError, ImportError) as e:
            raise CommandError("%s. Are you sure your INSTALLED_APPS setting is correct?" % e)
        output = []
        for app_config in app_configs:
            app_output = self.handle_app_config(app_config, **options)
            if app_output:
                output.append(app_output)
        return '\n'.join(output)

    def handle_app_config(self, app_config, **options):
        """
        Perform the command's actions for app_config, an AppConfig instance
        corresponding to an application label given on the command line.
        """
        raise NotImplementedError(
            "Subclasses of AppCommand must provide"
            "a handle_app_config() method.")


class LabelCommand(BaseCommand):
    """
    A management command which takes one or more arbitrary arguments
    (labels) on the command line, and does something with each of
    them.

    Rather than implementing ``handle()``, subclasses must implement
    ``handle_label()``, which will be called once for each label.

    If the arguments should be names of installed applications, use
    ``AppCommand`` instead.
    """
    label = 'label'
    missing_args_message = "Enter at least one %s." % label

    def add_arguments(self, parser):
        parser.add_argument('args', metavar=self.label, nargs='+')

    def handle(self, *labels, **options):
        output = []
        for label in labels:
            label_output = self.handle_label(label, **options)
            if label_output:
                output.append(label_output)
        return '\n'.join(output)

    def handle_label(self, label, **options):
        """
        Perform the command's actions for ``label``, which will be the
        string as given on the command line.
        """
        raise NotImplementedError('subclasses of LabelCommand must provide a handle_label() method')

class TemplateCommand(BaseCommand):
    """
    Copy either a Django application layout template or a Django project
    layout template into the specified directory.

    :param style: A color style object (see django.core.management.color).
    :param app_or_project: The string 'app' or 'project'.
    :param name: The name of the application or project.
    :param directory: The directory to which the template should be copied.
    :param options: The additional variables passed to project or app templates
    """
    requires_system_checks = False
    # The supported URL schemes
    url_schemes = ['http', 'https', 'ftp']
    # Rewrite the following suffixes when determining the target filename.
    rewrite_template_suffixes = (
        # Allow shipping invalid .py files without byte-compilation.
        ('.py-tpl', '.py'),
    )

    def add_arguments(self, parser):
        parser.add_argument('name', help='Name of the application or project.')
        parser.add_argument('directory', nargs='?', help='Optional destination directory')
        parser.add_argument('--template', help='The path or URL to load the template from.')
        parser.add_argument(
            '--extension', '-e', dest='extensions',
            action='append', default=['py'],
            help='The file extension(s) to render (default: "py"). '
                 'Separate multiple extensions with commas, or use '
                 '-e multiple times.'
        )
        parser.add_argument(
            '--name', '-n', dest='files',
            action='append', default=[],
            help='The file name(s) to render. Separate multiple file names '
                 'with commas, or use -n multiple times.'
        )

    def handle(self, app_or_project, name, target=None, **options):
        self.app_or_project = app_or_project
        self.paths_to_remove = []
        # self.verbosity = options['verbosity']

        self.validate_name(name, app_or_project)

        # if some directory is given, make sure it's nicely expanded
        if target is None:
            top_dir = os.path.join(os.getcwd(), name)
            try:
                os.makedirs(top_dir)
            except FileExistsError:
                raise CommandError("'%s' already exists" % top_dir)
            except OSError as e:
                raise CommandError(e)
        else:
            top_dir = os.path.abspath(os.path.expanduser(target))
            if not os.path.exists(top_dir):
                raise CommandError("Destination directory '%s' does not "
                                   "exist, please create it first." % top_dir)

        extensions = tuple(handle_extensions(options['extensions']))
        extra_files = []
        for file in options['files']:
            extra_files.extend(map(lambda x: x.strip(), file.split(',')))
        # if self.verbosity >= 2:
        #     self.stdout.write("Rendering %s template files with "
        #                       "extensions: %s\n" %
        #                       (app_or_project, ', '.join(extensions)))
        #     self.stdout.write("Rendering %s template files with "
        #                       "filenames: %s\n" %
        #                       (app_or_project, ', '.join(extra_files)))

        base_name = '%s_name' % app_or_project
        base_subdir = '%s_template' % app_or_project
        base_directory = '%s_directory' % app_or_project
        camel_case_name = 'camel_case_%s_name' % app_or_project
        camel_case_value = ''.join(x for x in name.title() if x != '_')

        context = Context({
            **options,
            base_name: name,
            base_directory: top_dir,
            camel_case_name: camel_case_value,
            # 'docs_version': get_docs_version(),
            'docs_version': '0.0.1',
            'django_version': feke.get_version(),
        }, autoescape=False)

        # Setup a stub settings environment for template rendering
        # if not settings.configured:
        #     settings.configure()
        #     django.setup()

        template_dir = self.handle_template(options['template'],
                                            base_subdir)
        prefix_length = len(template_dir) + 1

        for root, dirs, files in os.walk(template_dir):

            path_rest = root[prefix_length:]
            relative_dir = path_rest.replace(base_name, name)
            if relative_dir:
                target_dir = os.path.join(top_dir, relative_dir)
                if not os.path.exists(target_dir):
                    os.mkdir(target_dir)

            for dirname in dirs[:]:
                if dirname.startswith('.') or dirname == '__pycache__':
                    dirs.remove(dirname)

            for filename in files:
                if filename.endswith(('.pyo', '.pyc', '.py.class')):
                    # Ignore some files as they cause various breakages.
                    continue
                old_path = os.path.join(root, filename)
                new_path = os.path.join(top_dir, relative_dir,
                                     filename.replace(base_name, name))
                for old_suffix, new_suffix in self.rewrite_template_suffixes:
                    if new_path.endswith(old_suffix):
                        new_path = new_path[:-len(old_suffix)] + new_suffix
                        break  # Only rewrite once

                if os.path.exists(new_path):
                    raise CommandError("%s already exists, overlaying a "
                                       "project or app into an existing "
                                       "directory won't replace conflicting "
                                       "files" % new_path)

                # Only render the Python files, as we don't want to
                # accidentally render Django templates files
                if new_path.endswith(extensions) or filename in extra_files:
                    with open(old_path, 'r', encoding='utf-8') as template_file:
                        content = template_file.read()
                    template = Engine().from_string(content)
                    content = template.render(context)
                    with open(new_path, 'w', encoding='utf-8') as new_file:
                        new_file.write(content)
                else:
                    shutil.copyfile(old_path, new_path)

                # if self.verbosity >= 2:
                #     self.stdout.write("Creating %s\n" % new_path)
                try:
                    shutil.copymode(old_path, new_path)
                    self.make_writeable(new_path)
                except OSError:
                    self.stderr.write(
                        "Notice: Couldn't set permission bits on %s. You're "
                        "probably using an uncommon filesystem setup. No "
                        "problem." % new_path, self.style.NOTICE)

        if self.paths_to_remove:
            # if self.verbosity >= 2:
            #     self.stdout.write("Cleaning up temporary files.\n")
            for path_to_remove in self.paths_to_remove:
                if os.path.isfile(path_to_remove):
                    os.remove(path_to_remove)
                else:
                    shutil.rmtree(path_to_remove)

    def handle_template(self, template, subdir):
        """
        Determine where the app or project templates are.
        Use django.__path__[0] as the default because the Django install
        directory isn't known.
        """
        if template is None:
            return os.path.join(feke.core.__path__[0], 'conf', subdir)
        else:
            if template.startswith('file://'):
                template = template[7:]
            expanded_template = os.path.expanduser(template)
            expanded_template = os.path.normpath(expanded_template)
            if os.path.isdir(expanded_template):
                return expanded_template
            if self.is_url(template):
                # downloads the file and returns the path
                absolute_path = self.download(template)
            else:
                absolute_path = os.path.abspath(expanded_template)
            if os.path.exists(absolute_path):
                return self.extract(absolute_path)

        raise CommandError("couldn't handle %s template %s." %
                           (self.app_or_project, template))

    def validate_name(self, name, app_or_project):
        a_or_an = 'an' if app_or_project == 'app' else 'a'
        if name is None:
            raise CommandError('you must provide {an} {app} name'.format(
                an=a_or_an,
                app=app_or_project,
            ))
        # Check it's a valid directory name.
        if not name.isidentifier():
            raise CommandError(
                "'{name}' is not a valid {app} name. Please make sure the "
                "name is a valid identifier.".format(
                    name=name,
                    app=app_or_project,
                )
            )
        # Check it cannot be imported.
        try:
            import_module(name)
        except ImportError:
            pass
        else:
            raise CommandError(
                "'{name}' conflicts with the name of an existing Python "
                "module and cannot be used as {an} {app} name. Please try "
                "another name.".format(
                    name=name,
                    an=a_or_an,
                    app=app_or_project,
                )
            )

    def download(self, url):
        """
        Download the given URL and return the file name.
        """

        def cleanup_url(url):
            tmp = url.rstrip('/')
            filename = tmp.split('/')[-1]
            if url.endswith('/'):
                display_url = tmp + '/'
            else:
                display_url = url
            return filename, display_url

        prefix = 'django_%s_template_' % self.app_or_project
        tempdir = tempfile.mkdtemp(prefix=prefix, suffix='_download')
        self.paths_to_remove.append(tempdir)
        filename, display_url = cleanup_url(url)

        # if self.verbosity >= 2:
        #     self.stdout.write("Downloading %s\n" % display_url)
        try:
            the_path, info = urlretrieve(url, os.path.join(tempdir, filename))
        except IOError as e:
            raise CommandError("couldn't download URL %s to %s: %s" %
                               (url, filename, e))

        used_name = the_path.split('/')[-1]

        # Trying to get better name from response headers
        content_disposition = info.get('content-disposition')
        if content_disposition:
            _, params = cgi.parse_header(content_disposition)
            guessed_filename = params.get('filename') or used_name
        else:
            guessed_filename = used_name

        # Falling back to content type guessing
        ext = self.splitext(guessed_filename)[1]
        content_type = info.get('content-type')
        if not ext and content_type:
            ext = mimetypes.guess_extension(content_type)
            if ext:
                guessed_filename += ext

        # Move the temporary file to a filename that has better
        # chances of being recognized by the archive utils
        if used_name != guessed_filename:
            guessed_path = os.path.join(tempdir, guessed_filename)
            shutil.move(the_path, guessed_path)
            return guessed_path

        # Giving up
        return the_path

    def splitext(self, the_path):
        """
        Like os.path.splitext, but takes off .tar, too
        """
        base, ext = posixpath.splitext(the_path)
        if base.lower().endswith('.tar'):
            ext = base[-4:] + ext
            base = base[:-4]
        return base, ext

    def extract(self, filename):
        """
        Extract the given file to a temporarily and return
        the path of the directory with the extracted content.
        """
        prefix = 'django_%s_template_' % self.app_or_project
        tempdir = tempfile.mkdtemp(prefix=prefix, suffix='_extract')
        self.paths_to_remove.append(tempdir)
        # if self.verbosity >= 2:
        #     self.stdout.write("Extracting %s\n" % filename)
        try:
            archive.extract(filename, tempdir)
            return tempdir
        except (archive.ArchiveException, IOError) as e:
            raise CommandError("couldn't extract file %s to %s: %s" %
                               (filename, tempdir, e))

    def is_url(self, template):
        """Return True if the name looks like a URL."""
        if ':' not in template:
            return False
        scheme = template.split(':', 1)[0].lower()
        return scheme in self.url_schemes

    def make_writeable(self, filename):
        """
        Make sure that the file is writeable.
        Useful if our source is read-only.
        """
        if not os.access(filename, os.W_OK):
            st = os.stat(filename)
            new_permissions = stat.S_IMODE(st.st_mode) | stat.S_IWUSR
            os.chmod(filename, new_permissions)
