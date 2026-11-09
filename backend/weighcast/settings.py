import logging
from pathlib import Path
from typing import Any

import environ
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BASE_DIR.parent

env = environ.Env(
    DJANGO_DEBUG=(bool, False),
    DJANGO_ALLOWED_HOSTS=(list, ["localhost", "127.0.0.1"]),
    CORS_ALLOWED_ORIGINS=(list, ["http://localhost:5173", "http://127.0.0.1:5173"]),
    CSRF_TRUSTED_ORIGINS=(list, []),
    REDIS_URL=(str, ""),
    OPEN_METEO_BASE_URL=(str, "https://api.open-meteo.com/v1/forecast"),
    OPEN_METEO_API_KEY=(str, ""),
    VALHALLA_URL=(str, "https://valhalla1.openstreetmap.de/route"),
    LOG_LEVEL=(str, "INFO"),
    HEATMAP_MAX_CELLS=(int, 600),
)

for env_file in (REPO_ROOT / ".env", BASE_DIR / ".env"):
    if env_file.is_file():
        environ.Env.read_env(env_file)

DEBUG: bool = env("DJANGO_DEBUG")
SECRET_KEY: str = env(
    "DJANGO_SECRET_KEY",
    default="django-insecure-local-dev-only" if DEBUG else environ.Env.NOTSET,
)
ALLOWED_HOSTS: list[str] = env("DJANGO_ALLOWED_HOSTS")

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "corsheaders",
    "rest_framework",
    "drf_spectacular",
    "weighcast.core",
    "weighcast.routing",
    "weighcast.weather",
    "weighcast.api",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "weighcast.urls"
ASGI_APPLICATION = "weighcast.asgi.application"

TEMPLATES: list[dict[str, Any]] = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
    }
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

REDIS_URL: str = env("REDIS_URL")
# Production is Redis-first: shared weather/plan cache across workers. LocMem is
# local-dev / tests only — without Redis, Open-Meteo 429s come back on every cold start.
if not DEBUG and not REDIS_URL:
    raise ImproperlyConfigured(
        "REDIS_URL is required when DJANGO_DEBUG=false (Redis-first Open-Meteo cache)."
    )

CACHE_BACKEND = (
    "django.core.cache.backends.redis.RedisCache"
    if REDIS_URL
    else "django.core.cache.backends.locmem.LocMemCache"
)
CACHES: dict[str, dict[str, Any]] = {
    "default": (
        {
            "BACKEND": CACHE_BACKEND,
            "LOCATION": REDIS_URL,
            "TIMEOUT": 6 * 60 * 60,
            "KEY_PREFIX": "weighcast",
        }
        if REDIS_URL
        else {
            "BACKEND": CACHE_BACKEND,
            "LOCATION": "weighcast",
            "TIMEOUT": 6 * 60 * 60,
            "OPTIONS": {"MAX_ENTRIES": 20_000},
        }
    )
}
if REDIS_URL:
    logging.getLogger(__name__).info("Cache backend: Redis (%s)", REDIS_URL.split("@")[-1])
else:
    logging.getLogger(__name__).warning(
        "Cache backend: LocMem — set REDIS_URL for shared Open-Meteo caching"
    )

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = False
USE_TZ = True

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

CORS_ALLOWED_ORIGINS: list[str] = env("CORS_ALLOWED_ORIGINS")
CSRF_TRUSTED_ORIGINS: list[str] = env("CSRF_TRUSTED_ORIGINS")

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    "UNAUTHENTICATED_USER": None,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_THROTTLE_RATES": {
        "plan": "20/min",
        "heatmap": "30/min",
        "geocode": "120/min",
    },
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Weighcast API",
    "DESCRIPTION": "Weather-ranked truck routing",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

OPEN_METEO_BASE_URL: str = env("OPEN_METEO_BASE_URL")
OPEN_METEO_API_KEY: str = env("OPEN_METEO_API_KEY")
VALHALLA_URL: str = env("VALHALLA_URL")
HEATMAP_MAX_CELLS: int = env("HEATMAP_MAX_CELLS")

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SECURE_REFERRER_POLICY = "same-origin"
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "simple"},
    },
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL")},
    "loggers": {
        "httpx": {"level": "WARNING"},
        "httpcore": {"level": "WARNING"},
    },
}
