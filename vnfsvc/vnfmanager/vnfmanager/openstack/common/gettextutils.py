# Copyright 2012 Red Hat, Inc.
# Copyright 2013 IBM Corp.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
gettext for openstack-common modules.

Usual usage in an openstack.common module:

    from vnfmanager.openstack.common.gettextutils import _
"""

import copy
import functools
import gettext
import locale
from logging import handlers
import os

from babel import localedata
import six


# FIXME(dhellmann): Remove this when moving to oslo.i18n.
USE_LAZY = False


class TranslatorFactory(object):
    """Create translator functions
    """

    def __init__(self, domain, lazy=False, localedir=None):
        """Establish a set of translation functions for the domain.

        :param domain: Name of translation domain,
                       specifying a message catalog.
        :type domain: str
        :param lazy: Delays translation until a message is emitted.
                     Defaults to False.
        :type lazy: Boolean
        :param localedir: Directory with translation catalogs.
        :type localedir: str
        """
        self.domain = domain
        self.lazy = lazy
        if localedir is None:
            localedir = os.environ.get(domain.upper() + '_LOCALEDIR')
        self.localedir = localedir

    def _make_translation_func(self, domain=None):
        """Return a new translation function ready for use.

        Takes into account whether or not lazy translation is being
        done.

        The domain can be specified to override the default from the
        factory, but the localedir from the factory is always used
        because we assume the log-level translation catalogs are
        installed in the same directory as the main application
        catalog.

        """
        if domain is None:
            domain = self.domain
        if self.lazy:
            return functools.partial(Message, domain=domain)
        t = gettext.translation(
            domain,
            localedir=self.localedir,
            fallback=True,
        )
        if six.PY3:
            return t.gettext
        return t.ugettext

    @property
    def primary(self):
        "The default translation function."
        return self._make_translation_func()

    def _make_log_translation_func(self, level):
        return self._make_translation_func(self.domain + '-log-' + level)

    @property
    def log_info(self):
        "Translate info-level log messages."
        return self._make_log_translation_func('info')

    @property
    def log_warning(self):
        "Translate warning-level log messages."
        return self._make_log_translation_func('warning')

    @property
    def log_error(self):
        "Translate error-level log messages."
        return self._make_log_translation_func('error')

    @property
    def log_critical(self):
        "Translate critical-level log messages."
        return self._make_log_translation_func('critical')


# NOTE(dhellmann): When this module moves out of the incubator into
# oslo.i18n, these global variables can be moved to an integration
# module within each application.

# Create the global translation functions.
_translators = TranslatorFactory('vnf-mamager')

# The primary translation function using the well-known name "_"
_ = _translators.primary

# Translators for log levels.
#
# The abbreviated names are meant to reflect the usual use of a short
# name like '_'. The "L" is for "log" and the other letter comes from
# the level.
_LI = _translators.log_info
_LW = _translators.log_warning
_LE = _translators.log_error
_LC = _translators.log_critical

# NOTE(dhellmann): End of globals that will move to the application's
# integration module.

