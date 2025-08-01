"""
Django settings for the Pulp Platform application

Never import this module directly, instead `from django.conf import settings`, see
<https://docs.djangoproject.com/en/3.2/topics/settings/#using-settings-in-python-code>

For the full list of settings and their values, see
<https://docs.djangoproject.com/en/3.2/ref/settings/>
"""

import sys

from contextlib import suppress
from importlib import import_module
from logging import getLogger
from pathlib import Path

from cryptography.fernet import Fernet
from django.core.files.storage import storages
from django.conf import global_settings
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from pulpcore.app.loggers import deprecation_logger
from dynaconf import DjangoDynaconf, Dynaconf, Validator, get_history

from pulpcore import constants


try:
    import sentry_sdk

    sentry_sdk.init()
except ImportError:
    pass

if sys.version_info < (3, 10):
    # Python 3.9 has a rather different interface for `entry_points`.
    # Let's use a compatibility version.
    from importlib_metadata import entry_points
else:
    from importlib.metadata import entry_points

# Load settings first pass before applying all the defaults to get a grip on ENABLED_PLUGINS.
enabled_plugins_validator = Validator(
    "ENABLED_PLUGINS",
    is_type_of=list,
    len_min=1,
    when=Validator("ENABLED_PLUGINS", must_exist=True),
)

preload_settings = Dynaconf(
    ENVVAR_PREFIX_FOR_DYNACONF="PULP",
    ENV_SWITCHER_FOR_DYNACONF="PULP_ENV",
    ENVVAR_FOR_DYNACONF="PULP_SETTINGS",
    load_dotenv=False,
    validators=[
        enabled_plugins_validator,
    ],
)

# Build paths inside the project like this: BASE_DIR / ...
BASE_DIR = Path(__file__).absolute().parent

# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/3.2/howto/deployment/checklist/

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = False

ALLOWED_HOSTS = ["*"]

DEPLOY_ROOT = Path("/var/lib/pulp")
MEDIA_ROOT = str(DEPLOY_ROOT / "media")  # Django 3.1 adds support for pathlib.Path

# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/3.2/howto/static-files/

STATIC_URL = "/assets/"
STATIC_ROOT = DEPLOY_ROOT / STATIC_URL.strip("/")

# begin compatibility layer for DEFAULT_FILE_STORAGE
# Remove on pulpcore=3.85 or pulpcore=4.0

# - What is this?
# We shouldn't use STORAGES or DEFAULT_FILE_STORAGE directly because those are
# mutually exclusive by django, which constraints users to use whatever we use.
# This is a hack/workaround to set Pulp's default while still enabling users to choose
# the legacy or the new storage setting.
_DEFAULT_FILE_STORAGE = "pulpcore.app.models.storage.FileSystem"
_STORAGES = {
    "default": {
        "BACKEND": "pulpcore.app.models.storage.FileSystem",
    },
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

setattr(global_settings, "DEFAULT_FILE_STORAGE", _DEFAULT_FILE_STORAGE)
setattr(global_settings, "STORAGES", _STORAGES)
# end DEFAULT_FILE_STORAGE deprecation layer

REDIRECT_TO_OBJECT_STORAGE = True

WORKING_DIRECTORY = DEPLOY_ROOT / "tmp"
FILE_UPLOAD_TEMP_DIR = WORKING_DIRECTORY

CHUNKED_UPLOAD_DIR = "upload"

# List of upload handler classes to be applied in order.
FILE_UPLOAD_HANDLERS = ("pulpcore.app.files.HashingFileUploadHandler",)

# SECURITY WARNING: this should be set to a unique, unpredictable value
SECRET_KEY = "SECRET"

# Key used to encrypt fields in the database
DB_ENCRYPTION_KEY = "/etc/pulp/certs/database_fields.symmetric.key"

# API Root
API_ROOT = "/pulp/"
API_ROOT_REWRITE_HEADER = None

# Application definition

INSTALLED_APPS = [
    # django stuff
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
    "import_export",
    # third-party
    "django_filters",
    "django_guid",
    "drf_spectacular",
    "rest_framework",
    # pulp core app
    "pulpcore.app",
]

# Optional apps that help with development, or augment Pulp in some non-critical way
OPTIONAL_APPS = [
    "crispy_forms",
    "django_extensions",
    "storages",
]

for app in OPTIONAL_APPS:
    # only import if app is installed
    with suppress(ImportError):
        import_module(app)
        INSTALLED_APPS.append(app)

# Select enabled plugins and prepare to load their settings.
enabled_plugins = preload_settings.get("ENABLED_PLUGINS", None)
plugin_settings = []
for entry_point in entry_points(group="pulpcore.plugin"):
    if enabled_plugins is None or entry_point.name in enabled_plugins:
        plugin_app = entry_point.load()
        plugin_settings.append(f"{entry_point.module}.app.settings")
        INSTALLED_APPS += [plugin_app]

MIDDLEWARE = [
    "django_guid.middleware.guid_middleware",
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "pulpcore.middleware.DomainMiddleware",
    "pulpcore.middleware.APIRootRewriteMiddleware",
    "pulpcore.middleware.TaskProfilerMiddleware",
]

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "pulpcore.backends.ObjectRolePermissionBackend",
]

ROOT_URLCONF = "pulpcore.app.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "pulpcore.app.wsgi.application"

REST_FRAMEWORK = {
    "URL_FIELD_NAME": "pulp_href",
    "DEFAULT_FILTER_BACKENDS": ("pulpcore.filters.PulpFilterBackend",),
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "PAGE_SIZE": 100,
    "DEFAULT_PERMISSION_CLASSES": ("pulpcore.app.access_policy.AccessPolicyFromDB",),
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "pulpcore.app.authentication.BasicAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ),
    "UPLOADED_FILES_USE_URL": False,
    "DEFAULT_VERSIONING_CLASS": "rest_framework.versioning.URLPathVersioning",
    "DEFAULT_SCHEMA_CLASS": "pulpcore.openapi.PulpAutoSchema",
}

# Password validation
# https://docs.djangoproject.com/en/3.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# Internationalization
# https://docs.djangoproject.com/en/3.2/topics/i18n/

LANGUAGE_CODE = "en-us"

TIME_ZONE = "UTC"

USE_I18N = "USE_I18N", True

USE_L10N = True

USE_TZ = True


# A set of default settings to use if the configuration file in
# /etc/pulp/ is missing or if it does not have values for every setting

# https://docs.djangoproject.com/en/3.2/ref/settings/#databases
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "pulp",
        "USER": "pulp",
        "CONN_MAX_AGE": 0,
    },
}

# Redis default config
REDIS_URL = None
REDIS_HOST = None
REDIS_PORT = None
REDIS_DB = 0
REDIS_PASSWORD = None
REDIS_SSL = False
REDIS_SSL_CA_CERTS = None

# Social_django settings
SOCIAL_AUTH_URL_NAMESPACE = "social"
SOCIAL_AUTH_POSTGRES_JSONFIELD = True
SOCIAL_AUTH_JSONFIELD_CUSTOM = "django.db.models.JSONField"

# https://docs.djangoproject.com/en/3.2/ref/settings/#logging and
# https://docs.python.org/3/library/logging.config.html
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "pulp [%(correlation_id)s]: %(name)s:%(levelname)s: %(message)s"}
    },
    "filters": {"correlation_id": {"()": "django_guid.log_filters.CorrelationId"}},
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "simple",
            "filters": ["correlation_id"],
        }
    },
    "loggers": {
        "": {
            # The root logger
            "handlers": ["console"],
            "level": "INFO",
            "filters": ["correlation_id"],
        },
        "django_guid": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}

# Custom access policies indexed by the url_pattern of the view.
# This will only take effect when combined with the
# `pulpcore.app.access_policy.AccessPoliciesFromSettings` permission class.
ACCESS_POLICIES = {}

DRF_ACCESS_POLICY = {"reusable_conditions": ["pulpcore.app.global_access_conditions"]}

CONTENT_ORIGIN = None
CONTENT_PATH_PREFIX = "/pulp/content/"

API_APP_TTL = 120  # The heartbeat is called from gunicorn notify (defaulting to 45 sec).
CONTENT_APP_TTL = 30
WORKER_TTL = 30

# Seconds for a task to finish on semi graceful worker shutdown (approx)
# On SIGHUP, SIGTERM the currently running task will be awaited forever.
# On SIGINT, this value represents the time before the worker will attempt to kill the subprocess.
TASK_GRACE_INTERVAL = 600

# how long to protect ephemeral items in minutes
ORPHAN_PROTECTION_TIME = 24 * 60

# Custom cleanup intervals
# for the following, if set to 0, the corresponding cleanup task is disabled
UPLOAD_PROTECTION_TIME = 0
TASK_PROTECTION_TIME = 0
TMPFILE_PROTECTION_TIME = 0

REMOTE_USER_ENVIRON_NAME = "REMOTE_USER"

AUTHENTICATION_JSON_HEADER = ""
AUTHENTICATION_JSON_HEADER_JQ_FILTER = ""
AUTHENTICATION_JSON_HEADER_OPENAPI_SECURITY_SCHEME = {}

ALLOWED_IMPORT_PATHS = []

ALLOWED_EXPORT_PATHS = []

# https://pulpproject.org/pulpcore/docs/admin/reference/settings/?h=settings#cache_enabled
CACHE_ENABLED = False
CACHE_SETTINGS = {
    "EXPIRES_TTL": 600,  # 10 minutes
}

# The time in seconds a RemoteArtifact will be ignored after failure.
REMOTE_CONTENT_FETCH_FAILURE_COOLDOWN = 5 * 60  # 5 minutes

SPECTACULAR_SETTINGS = {
    "OAS_VERSION": "3.1.1",
    "SERVE_URLCONF": ROOT_URLCONF,
    "DEFAULT_GENERATOR_CLASS": "pulpcore.openapi.PulpSchemaGenerator",
    "DEFAULT_SCHEMA_CLASS": "pulpcore.openapi.PulpAutoSchema",
    "ENUM_ADD_EXPLICIT_BLANK_NULL_CHOICE": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "COMPONENT_NO_READ_ONLY_REQUIRED": True,
    "GENERIC_ADDITIONAL_PROPERTIES": None,
    "DISABLE_ERRORS_AND_WARNINGS": not DEBUG,
    "POSTPROCESSING_HOOKS": [
        "drf_spectacular.hooks.postprocess_schema_enums",
        "pulpcore.openapi.hooks.add_info_hook",
    ],
    "TITLE": "Pulp 3 API",
    "DESCRIPTION": "Fetch, Upload, Organize, and Distribute Software Packages",
    "VERSION": "v3",
    "CONTACT": {
        "name": "Pulp Team",
        "email": "pulp-list@redhat.com",
        "url": "https://pulpproject.org",
    },
    "LICENSE": {
        "name": "GPLv2+",
        "url": "https://raw.githubusercontent.com/pulp/pulpcore/master/LICENSE",
    },
}

# What kinds of checksums is this pulp-instance _allowed to use_ ?
# NOTE : "sha256"" IS REQUIRED - Pulp will fail to start if it is not found in this set
# NOTE: specifying checksums that are not listed under ALL_KNOWN_CONTENT_CHECKSUMS will fail
#       at startup
ALLOWED_CONTENT_CHECKSUMS = ["sha224", "sha256", "sha384", "sha512"]

DEFAULT_AUTO_FIELD = "django.db.models.AutoField"

# Possible diagnostics:
# * "memory"
#    Logs the task process RSS every couple of seconds
# * "pyinstrument"
#    Dumps an HTML profile report produced by pyinstrument, showing time spent in various
#    callstacks. This adds ~10% overhead to the task process and consumes extra memory.
#    Tweaking code might be warranted for some advanced settings.
# * "memray" - Dumps a report produced by memray which logs how much memory was allocated by which
#    lines and functions, at the time of peak RSS of the task process. This adds significant
#    runtime overhead to the task process, 20-40%. Tweaking code might be warranted for
#    some advanced settings.
# NOTE: "memray" and "pyinstrument" require additional packages to be installed on the system.
TASK_DIAGNOSTICS = []  # ["memory", "pyinstrument", "memray"]

ANALYTICS = True

HIDE_GUARDED_DISTRIBUTIONS = False

DOMAIN_ENABLED = False

SHELL_PLUS_IMPORTS = [
    "from pulpcore.app.util import get_domain, get_domain_pk, set_domain, get_url, extract_pk",
    "from pulpcore.tasking.tasks import dispatch, cancel_task, wakeup_worker",
]

# What percentage of available-workers will pulpimport use at a time, max
# By default, use all available workers.
IMPORT_WORKERS_PERCENT = 100

# Kafka settings
KAFKA_BOOTSTRAP_SERVERS = None  # kafka integration disabled by default
KAFKA_TASKS_STATUS_TOPIC = "pulpcore.tasking.status"
KAFKA_TASKS_STATUS_PRODUCER_SYNC_ENABLED = False
KAFKA_PRODUCER_POLL_TIMEOUT = 0.1
KAFKA_SECURITY_PROTOCOL = "plaintext"
KAFKA_SSL_CA_PEM = None
KAFKA_SASL_MECHANISM = None
KAFKA_SASL_USERNAME = None
KAFKA_SASL_PASSWORD = None

# opentelemetry settings
OTEL_ENABLED = False

# Replaces asyncio event loop with uvloop
UVLOOP_ENABLED = False

# HERE STARTS DYNACONF EXTENSION LOAD (Keep at the very bottom of settings.py)
# Read more at https://www.dynaconf.com/django/

# Validators

storage_keys = ("STORAGES.default.BACKEND", "DEFAULT_FILE_STORAGE")
storage_validator = (
    Validator("REDIRECT_TO_OBJECT_STORAGE", eq=False)
    | Validator(*storage_keys, eq="pulpcore.app.models.storage.FileSystem")
    | Validator(*storage_keys, eq="storages.backends.azure_storage.AzureStorage")
    | Validator(*storage_keys, eq="storages.backends.s3.S3Storage")
    | Validator(*storage_keys, eq="storages.backends.s3boto3.S3Boto3Storage")
    | Validator(*storage_keys, eq="storages.backends.gcloud.GoogleCloudStorage")
)
storage_validator.messages["combined"] = (
    "'REDIRECT_TO_OBJECT_STORAGE=True' is only supported with the local file, S3, GCP or Azure "
    "storage backend configured in STORAGES['default']['BACKEND'] "
    "(deprecated DEFAULT_FILE_STORAGE)."
)

cache_enabled_validator = Validator("CACHE_ENABLED", eq=True)
redis_url_validator = Validator("REDIS_URL", must_exist=True, when=cache_enabled_validator)
redis_host_validator = Validator("REDIS_HOST", must_exist=True, when=cache_enabled_validator)
redis_port_validator = Validator("REDIS_PORT", must_exist=True, when=cache_enabled_validator)
cache_validator = redis_url_validator | (redis_host_validator & redis_port_validator)
cache_validator.messages["combined"] = (
    "CACHE_ENABLED is enabled but it requires to have REDIS configured. Please check "
    "https://pulpproject.org/pulpcore/docs/admin/reference/settings/?h=settings#redis-settings "
    "for more information."
)

sha256_validator = Validator(
    "ALLOWED_CONTENT_CHECKSUMS",
    cont="sha256",
    messages={
        "operations": "ALLOWED_CONTENT_CHECKSUMS MUST contain 'sha256' - Pulp's "
        "content addressable storage relies on sha256 to identify entities."
    },
)

unknown_algs_validator = Validator(
    "ALLOWED_CONTENT_CHECKSUMS",
    condition=lambda x: len(set(x).difference(constants.ALL_KNOWN_CONTENT_CHECKSUMS)) == 0,
    messages={
        "condition": (
            "ALLOWED_CONTENT_CHECKSUMS may only contain algorithms known to pulp - see "
            "constants.ALL_KNOWN_CONTENT_CHECKSUMS for the allowed list."
        )
    },
)

api_root_validator = Validator(
    "API_ROOT",
    condition=lambda x: x.startswith("/") and x.endswith("/"),
    messages={
        "condition": ("The API_ROOT must start and end with a '/', currently it is '{value}'")
    },
)

json_header_auth_class_restframework_validator = Validator(
    "REST_FRAMEWORK__DEFAULT_AUTHENTICATION_CLASSES",
    cont="pulpcore.app.authentication.JSONHeaderRemoteAuthentication",
)

authentication_json_header_validator = Validator(
    "AUTHENTICATION_JSON_HEADER",
    startswith="HTTP_",
    must_exist=True,
    when=json_header_auth_class_restframework_validator,
    messages={"startswith": 'The AUTHENTICATION_JSON_HEADER must start with "HTTP_"'},
)

authentication_json_header_jq_filter_validator = Validator(
    "AUTHENTICATION_JSON_HEADER_JQ_FILTER",
    startswith=".",
    must_exist=True,
    when=json_header_auth_class_restframework_validator,
    messages={"startswith": 'The AUTHENTICATION_JSON_HEADER_JQ_FILTER must start with "."'},
)

json_header_auth_validator = (
    authentication_json_header_validator & authentication_json_header_jq_filter_validator
)

authentication_json_header_openapi_security_scheme_setting_validator = Validator(
    "AUTHENTICATION_JSON_HEADER_OPENAPI_SECURITY_SCHEME", len_min=1
)
authentication_json_header_openapi_security_scheme_validator = Validator(
    "AUTHENTICATION_JSON_HEADER_OPENAPI_SECURITY_SCHEME",
    when=authentication_json_header_openapi_security_scheme_setting_validator,
    is_type_of=dict,
    messages={"is_type_of": "{name} must be a dictionary."},
)


def otel_middleware_hook(settings):
    data = {"dynaconf_merge": True}
    if settings.OTEL_ENABLED:
        data["MIDDLEWARE"] = ["pulpcore.middleware.DjangoMetricsMiddleware"]
    return data


del preload_settings

settings = DjangoDynaconf(
    __name__,
    ENVVAR_PREFIX_FOR_DYNACONF="PULP",
    ENV_SWITCHER_FOR_DYNACONF="PULP_ENV",
    ENVVAR_FOR_DYNACONF="PULP_SETTINGS",
    # Ensure the plugin defaults are loaded before user configs.
    PRELOAD_FOR_DYNACONF=plugin_settings,
    load_dotenv=False,
    validators=[
        api_root_validator,
        cache_validator,
        sha256_validator,
        storage_validator,
        unknown_algs_validator,
        json_header_auth_validator,
        authentication_json_header_openapi_security_scheme_validator,
    ],
    post_hooks=(otel_middleware_hook,),
)

# begin compatibility layer for DEFAULT_FILE_STORAGE
# Remove on pulpcore=3.85 or pulpcore=4.0
using_deprecated_storage_settings = len(get_history(settings, key="DEFAULT_FILE_STORAGE")) > 1
if using_deprecated_storage_settings:
    deprecation_logger.warning(
        "[deprecation] DEFAULT_FILE_STORAGE will be removed in pulpcore 3.85. "
        "Learn how to upgrade to STORAGES:\n"
        "https://discourse.pulpproject.org/t/"
        "action-required-upgrade-your-storage-settings-before-pulpcore-3-85/2072/2"
    )
# Ensures the cached property storage.backends uses the the right value
storages._backends = settings.STORAGES.copy()
storages.backends
# end compatibility layer

_logger = getLogger(__name__)


if not (
    Path(sys.argv[0]).name in ["pytest", "sphinx-build"]
    or (len(sys.argv) >= 2 and sys.argv[1] in ["collectstatic", "openapi"])
):
    try:
        with open(DB_ENCRYPTION_KEY, "rb") as key_file:
            Fernet(key_file.read())
    except Exception as ex:
        raise ImproperlyConfigured(
            ("Could not load DB_ENCRYPTION_KEY file '{file}': {err}").format(
                file=DB_ENCRYPTION_KEY, err=ex
            )
        )


FORBIDDEN_CHECKSUMS = set(constants.ALL_KNOWN_CONTENT_CHECKSUMS).difference(
    ALLOWED_CONTENT_CHECKSUMS
)

_SKIPPED_COMMANDS_FOR_CONTENT_CHECKS = [
    "handle-artifact-checksums",
    "migrate",
    "collectstatic",
    "openapi",
]

if not (len(sys.argv) >= 2 and sys.argv[1] in _SKIPPED_COMMANDS_FOR_CONTENT_CHECKS):
    try:
        with connection.cursor() as cursor:
            for checksum in ALLOWED_CONTENT_CHECKSUMS:
                # can't import Artifact here so use a direct db connection
                cursor.execute(
                    f"SELECT count(pulp_id) FROM core_artifact WHERE {checksum} IS NULL LIMIT 1"
                )
                row = cursor.fetchone()
                if row[0] > 0:
                    raise ImproperlyConfigured(
                        (
                            "There have been identified artifacts missing checksum '{}'. "
                            "Run 'pulpcore-manager handle-artifact-checksums' first to populate "
                            "missing artifact checksums."
                        ).format(checksum)
                    )
            for checksum in FORBIDDEN_CHECKSUMS:
                # can't import Artifact here so use a direct db connection
                cursor.execute(
                    f"SELECT count(pulp_id) FROM core_artifact WHERE {checksum} IS NOT NULL LIMIT 1"
                )
                row = cursor.fetchone()
                if row[0] > 0:
                    raise ImproperlyConfigured(
                        (
                            "There have been identified artifacts with forbidden checksum '{}'. "
                            "Run 'pulpcore-manager handle-artifact-checksums' first to unset "
                            "forbidden checksums."
                        ).format(checksum)
                    )

            # warn if there are remote artifacts with checksums but no allowed checksums
            cond = " AND ".join([f"{c} IS NULL" for c in constants.ALL_KNOWN_CONTENT_CHECKSUMS])
            no_checksum_query = f"SELECT pulp_id FROM core_remoteartifact WHERE {cond}"
            cond = " AND ".join([f"{c} IS NULL" for c in ALLOWED_CONTENT_CHECKSUMS])
            cursor.execute(
                f"SELECT count(pulp_id) FROM core_remoteartifact WHERE {cond} AND "
                f"pulp_id NOT IN ({no_checksum_query}) LIMIT 1"
            )
            row = cursor.fetchone()
            if row[0] > 0:
                _logger.warning(
                    (
                        "Warning: detected remote content without allowed checksums. "
                        "Run 'pulpcore-manager handle-artifact-checksums --report' to "
                        "view this content."
                    )
                )

    except ImproperlyConfigured as e:
        raise e
    except Exception:
        # our check could fail if the table hasn't been created yet or we can't get a db connection
        pass
    finally:
        connection.close()

if settings.API_ROOT_REWRITE_HEADER:
    api_root = "/<path:api_root>/"
else:
    api_root = settings.API_ROOT
settings.set("V3_API_ROOT", api_root + "api/v3/")  # Not user configurable
settings.set("V3_DOMAIN_API_ROOT", api_root + "<slug:pulp_domain>/api/v3/")
settings.set("V3_API_ROOT_NO_FRONT_SLASH", settings.V3_API_ROOT.lstrip("/"))
settings.set("V3_DOMAIN_API_ROOT_NO_FRONT_SLASH", settings.V3_DOMAIN_API_ROOT.lstrip("/"))
