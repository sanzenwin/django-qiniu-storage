"""
Qiniu Storage Backends
"""
from __future__ import absolute_import, unicode_literals
import datetime
import os
import posixpath
import warnings

import six

try:
    from urllib.parse import urljoin, urlparse
except ImportError:
    from urlparse import urljoin, urlparse

from qiniu import Auth, BucketManager, put_data
import requests

from django.conf import settings
from django.core.files.base import File
from django.core.files.storage import Storage
from django.core.exceptions import ImproperlyConfigured, SuspiciousOperation

try:
    from django.utils.encoding import force_text
except ImportError:
    from django.utils.encoding import force_str as force_text
from django.utils.encoding import force_bytes, filepath_to_uri

from django.utils.deconstruct import deconstructible

from .utils import QiniuError, bucket_lister


def get_qiniu_config(name, default=None):
    """
    Get configuration variable from environment variable
    or django setting.py
    """
    config = os.environ.get(name, getattr(settings, name, default))
    if config is not None:
        if isinstance(config, six.string_types):
            return config.strip()
        else:
            return config
    else:
        raise ImproperlyConfigured(
            "Can't find config for '%s' either in environment"
            "variable or in setting.py" % name)


@deconstructible
class QiniuStorage(Storage):
    """
    Qiniu Storage Service
    """
    location = ""

    bucket_name_key = 'QINIU_BUCKET_NAME'
    user_domain_key = 'QINIU_BUCKET_DOMAIN'

    def __init__(self, **kwargs):
        self.auth = Auth(
            get_qiniu_config('QINIU_ACCESS_KEY'),
            get_qiniu_config('QINIU_SECRET_KEY')
        )
        self.bucket_name = get_qiniu_config(self.bucket_name_key)
        self.bucket_domain = get_qiniu_config(self.user_domain_key, '').rstrip('/')
        self.bucket_manager = BucketManager(self.auth)
        self.secure_url = get_qiniu_config('QINIU_SECURE_URL', 'False').lower() in ('true', '1')

    def _clean_name(self, name):
        """
        Cleans the name so that Windows style paths work
        """
        # Normalize Windows style paths
        clean_name = posixpath.normpath(name).replace('\\', '/')

        # os.path.normpath() can strip trailing slashes so we implement
        # a workaround here.
        if name.endswith('/') and not clean_name.endswith('/'):
            # Add a trailing slash as it was stripped.
            return clean_name + '/'
        else:
            return clean_name

    def _normalize_name(self, name):
        """
        Normalizes the name so that paths like /path/to/ignored/../foo.txt
        work. We check to make sure that the path pointed to is not outside
        the directory specified by the LOCATION setting.
        """

        base_path = force_text(self.location)
        base_path = base_path.rstrip('/')

        final_path = urljoin(base_path.rstrip('/') + "/", name)

        base_path_len = len(base_path)
        if (not final_path.startswith(base_path) or
                final_path[base_path_len:base_path_len + 1] not in ('', '/')):
            raise SuspiciousOperation("Attempted access to '%s' denied." %
                                      name)
        return final_path.lstrip('/')

    def _open(self, name, mode='rb'):
        return QiniuFile(name, self, mode)

    def _save(self, name, content):
        cleaned_name = self._clean_name(name)
        name = self._normalize_name(cleaned_name)

        if hasattr(content, 'chunks'):
            content_str = b''.join(chunk for chunk in content.chunks())
        else:
            content_str = content.read()

        self._put_file(name, content_str)
        return cleaned_name

    def _put_file(self, name, content):
        token = self.auth.upload_token(self.bucket_name)
        ret, info = put_data(token, name, content)
        if ret is None or ret['key'] != name:
            raise QiniuError(info)

    def _read(self, name):
        return requests.get(self.url(name)).content

    def delete(self, name):
        name = self._normalize_name(self._clean_name(name))
        if six.PY2:
            name = name.encode('utf-8')
        ret, info = self.bucket_manager.delete(self.bucket_name, name)

        if ret is None or info.status_code == 612:
            raise QiniuError(info)

    def _file_stat(self, name, silent=False):
        name = self._normalize_name(self._clean_name(name))
        if six.PY2:
            name = name.encode('utf-8')
        ret, info = self.bucket_manager.stat(self.bucket_name, name)
        if ret is None and not silent:
            raise QiniuError(info)
        return ret

    def exists(self, name):
        stats = self._file_stat(name, silent=True)
        return True if stats else False

    def size(self, name):
        stats = self._file_stat(name)
        return stats['fsize']

    def modified_time(self, name):
        stats = self._file_stat(name)
        time_stamp = float(stats['putTime']) / 10000000
        return datetime.datetime.fromtimestamp(time_stamp)

    def listdir(self, name):
        name = self._normalize_name(self._clean_name(name))
        if name and not name.endswith('/'):
            name += '/'

        dirlist = bucket_lister(self.bucket_manager, self.bucket_name,
                                prefix=name)
        files = []
        dirs = set()
        base_parts = name.split("/")[:-1]
        for item in dirlist:
            parts = item['key'].split("/")
            parts = parts[len(base_parts):]
            if len(parts) == 1:
                # File
                files.append(parts[0])
            elif len(parts) > 1:
                # Directory
                dirs.add(parts[0])
        return list(dirs), files

    def url(self, name):
        name = self._normalize_name(self._clean_name(name))
        name = filepath_to_uri(name)
        protocol = u'https://' if self.secure_url else u'http://'
        return urljoin(protocol + self.bucket_domain, name)


class QiniuMediaStorage(QiniuStorage):
    bucket_name_key = 'QINIU_BUCKET_NAME_MEDIA'
    user_domain_key = 'QINIU_BUCKET_DOMAIN_MEDIA'

    def __init__(self, *args, **kwargs):
        warnings.warn(
            "QiniuMediaStorage is deprecated, and will be removed in the future."
            "User uploads handled by QiniuMediaStorage are public and can be accessed without any checks."
            "For general use, please choose QiniuPrivateStorage instead."
            , DeprecationWarning)
        super(QiniuMediaStorage, self).__init__(*args, **kwargs)

    location = os.path.basename(settings.MEDIA_ROOT)


class QiniuStaticStorage(QiniuStorage):
    bucket_name_key = 'QINIU_BUCKET_NAME_STATIC'
    user_domain_key = 'QINIU_BUCKET_DOMAIN_STATIC'

    location = os.path.basename(settings.STATIC_ROOT) or "static"


class QiniuPrivateStorage(QiniuStorage):
    bucket_name_key = 'QINIU_BUCKET_NAME_MEDIA'
    user_domain_key = 'QINIU_BUCKET_DOMAIN_MEDIA'

    def url(self, name):
        raw_url = super(QiniuPrivateStorage, self).url(name)
        return force_text(self.auth.private_download_url(raw_url))


class QiniuFile(File):
    def __init__(self, name, storage, mode):
        self._storage = storage
        self._name = name[len(self._storage.location):].lstrip('/')
        self._mode = mode
        self.file = six.BytesIO()
        self._is_dirty = False
        self._is_read = False

    @property
    def size(self):
        if self._is_dirty or self._is_read:
            # Get the size of a file like object
            # Check http://stackoverflow.com/a/19079887
            old_file_position = self.file.tell()
            self.file.seek(0, os.SEEK_END)
            self._size = self.file.tell()
            self.file.seek(old_file_position, os.SEEK_SET)
        if not hasattr(self, '_size'):
            self._size = self._storage.size(self._name)
        return self._size

    def read(self, num_bytes=None):
        if not self._is_read:
            content = self._storage._read(self._name)
            self.file = six.BytesIO(content)
            self._is_read = True

        if num_bytes is None:
            data = self.file.read()
        else:
            data = self.file.read(num_bytes)

        if 'b' in self._mode:
            return data
        else:
            return force_text(data)

    def write(self, content):
        if 'w' not in self._mode:
            raise AttributeError("File was opened for read-only access.")

        self.file.write(force_bytes(content))
        self._is_dirty = True
        self._is_read = True

    def close(self):
        if self._is_dirty:
            self.file.seek(0)
            self._storage._save(self._name, self.file)
        self.file.close()
