# -*- coding: utf-8 -*-

"""
Copyright: (c) 2018 Glutanimate <https://glutanimate.com/>
License: GNU AGPLv3 <https://www.gnu.org/licenses/agpl.html>
"""

import os
import io
from copy import deepcopy

from anki.utils import json
from anki.hooks import addHook

from .utils import mergeDictsRecursively
from .platform import ANKI21, ADDON_PATH, ADDON_MODULE

DEFAULT_LOCAL_CONFIG_PATH = os.path.join(ADDON_PATH, "config.json")
DEFAULT_LOCAL_META_PATH = os.path.join(ADDON_PATH, "meta.json")


class ConfigError(Exception):
    """
    Thrown whenever a config-specific exception occurs
    """
    pass


class ConfigManager(object):

    """
    Generic configuration manager for Anki

    Supports the following configuration storages:

    name        location            data type     scope             notes
    ==========================================================================
    local       json files in       dictionary    all profiles      introduced
                add-on directory                                    in 2.1
    --------------------------------------------------------------------------
    synced      json string in      dictionary    user profile      limited
                collection.anki2                  (synced)          capacity
    --------------------------------------------------------------------------
    profile     pickle object       dictionary    user profile      limited
                in prefs.db                       (local)           capacity

    """

    _supported_storages = ("local", "synced", "profile")

    def __init__(self, mw, config_dict={"local": None},
                 conf_key=ADDON_MODULE, conf_action=None,
                 reset_req=False, preload=False):
        """
        Initialize a new config manager object with the provided storages

        Arguments:
            mw {QMainWindow} -- Anki main window object

        Keyword Arguments:
            config_dict {dict}:
                Dictionary of configuration storages. Supported keys are
                limited to the ones listed in _supported_storages. Each
                key, with the exception of the local storage type, should
                be mapped to a dictionary of default config values.
                There is no need to supply a default dictionary for the
                local storage type, as it will automatically be read
                from the config.json file.
                (default: {{"local": None}})
            conf_key {str}:
                Dictionary key to use when saving storage types that use Anki's
                databases. Set to the topmost add-on module name by default.
                (e.g. "review_heatmap")
                (default: {ADDON_MODULE})
            conf_action {function}:
                Function/method to call when user clicks on configure button
                (2.1-specific) (default: {None})
            reset_req {bool}:
                Whether we should fire a reset event when the
                configuration is saved (e.g. to update parts of Anki's UI)
                (default: {False})
            preload {bool}:
                Whether or not to load all available configuration storages
                at profile load time. By default storages will only
                be loaded on demand. (default: {False})
        """
        self.mw = mw
        self._reset_req = reset_req
        self._conf_key = conf_key
        self._storages = {
            key: {"default": value, "dirty": False, "loaded": False}
            for key, value in config_dict.items()
        }
        self._config = {}
        if "local" in self._storages:
            self._storages["local"]["default"] = self._getLocalDefaults()
            self._setupLocalHooks()
        self._setupSaveHooks()
        if ANKI21 and conf_action:
            self.setConfigAction(conf_action)
        if preload:
            self._maybeLoad()

    # Dictionary interface

    def __getitem__(self, name):
        """
        Implements evaluation of self[storage_name]

        storage_name needs to be in _supported_storages

        Attempts to load storage on demand if it has not been
        initialized, yet.

        Automatically falls back to defaults if no
        user-specific settings saved, yet.
        """
        self._checkStorage(name)
        try:
            config = self._config[name]
        except KeyError:
            # Attempt to load storage on demand
            self.load(storage_name=name)
            config = self._config[name]
        return config

    def __setitem__(self, name, value):
        """
        Implements assignment of self[storage_name]
        """
        self._checkStorage(name)
        self._config[name] = value
        self._storages[name]["dirty"] = True

    def __str__(self):
        """
        Returns printable representation of all config storage values.
        """
        return self._config.__str__()

    # Regular API

    def load(self, storage_name=None):
        """
        Load config values into ConfigManager.

        Automatically falls back to defaults if no
        user-specific settings saved, yet.

        Keyword Arguments:
            storage_name {str} -- Storage to load. Loads all storages if
                                  left blank (default: {None}).
        """
        for name in ([storage_name] if storage_name else self._storages):
            self._checkStorage(name)
            getter = getattr(self, "_get" + name.capitalize())
            self._config[name] = getter()
            self._storages[name]["loaded"] = True

    def save(self, storage_name=None, profile_unload=False):
        """
        Write config values to their corresponding storages.

        Automatically fires a reset event if reset_req=True.

        Keyword Arguments:
            storage_name {str} -- Storage to save. Saves all storages if
                                  left blank (default: {None}).
        """
        for name in ([storage_name] if storage_name else self._storages):
            self._checkStorage(name)
            saver = getattr(self, "_save" + name.capitalize())
            saver(self._config[name])
            self._storages[name]["dirty"] = True

        if self._reset_req and not profile_unload:
            self.mw.reset()

    @property
    def all(self):
        """
        Implements evaluation of self.all

        Returns the values of all config storages currently managed
        by the config manager instance.

        Returns:
            dict -- Dictionary of all config values
        """
        return self._config

    @all.setter
    def all(self, config_dict):
        """
        Implements assignment of self.all

        Allows updating all configuration values at once.

        Arguments:
            config_dict {dict}:
                Dictionary of config dictionaries
                (Same format as config_dict in __init__,
                only that the current config values should
                be provided instead of defaults)
        """
        self._config = config_dict
        self._storages = {
            name: {"default": {}, "dirty": False, "loaded": False} for name in config_dict
        }

    @property
    def defaults(self):
        """
        Implements evaluation of self.defaults

        Returns the default values of all config storages
        currently managed by the config manager instance.

        Returns:
            dict -- Dictionary of all default config values
        """
        return {name: storage_dict["default"]
                for name, storage_dict in self._storages.items()}

    @defaults.setter
    def defaults(self, config_dict):
        """
        Implements assignment of self.defaults

        Allows updating all default config values at once.

        Arguments:
            config_dict {dict}:
                Dictionary of default config dictionaries
                (Same format as config_dict in __init__)
        """
        for name in config_dict:
            self._storages[name]["default"] = config_dict[name]

    def restoreDefaults(self):
        """
        Restore all config values to the defaults and save storages
        """
        for name in self._storages:
            self._config[name] = self._storages[name]["default"]
        self.save()

    def onProfileUnload(self):
        """
        Write unsaved changes to the corresponding storages.
        """
        for name, storage_dict in self._storages.items():
            if not storage_dict["dirty"]:
                continue
            self.save(name)

    def setConfigAction(self, action):
        """
        Set function/method to call when user clicks on
        'Configure' button in Anki 2.1's add-on manager.

        Arguments:
            action {function} -- Function to call
        """
        self._conf_action = action
        self._setupConfigButtonHook(action)

    # General helper methods

    def _maybeLoad(self):
        """
        Try loading config storages, delegating loading until
        Anki profile is ready if necessary
        """
        if "synced" or "profile" in self._storages and self.mw.col is None:
            # Profile not ready. Defer config loading.
            addHook("profileLoaded", self.load)
            return
        self.load()

    def _checkStorage(self, name):
        """
        Checks whether provided storage name is supported and
        initialized in current ConfigManager instance

        Arguments:
            name {str} -- Storage name, as listed in _supported_storages

        Raises:
            NotImplementedError -- Config storage not implemented in class
            ConfigError -- Config storage not initialized in current
                           instance
        """
        if name not in self._supported_storages:
            raise NotImplementedError(
                "Config storage type not implemented in libaddon: ", name)
        elif name not in self._storages:
            raise ConfigError(
                "Config storage type not available for this add-on: ", name)

    def _setupSaveHooks(self):
        """
        Adds hooks for various events that should trigger saving the config
        """
        # Custom add-on-specifc hook that can be run by this/other add-ons
        addHook("config_changed_{}".format(ADDON_MODULE),
                self.save)
        # Hook run on unloading Anki profile. Ensures that any unsaved changes
        # are saved to the corresponding storages
        addHook("unloadProfile", self.onProfileUnload)

    def _setupConfigButtonHook(self, action):
        """
        Assigns provided function to Anki 2.1's "Configure" button

        Arguments:
            action {function} -- Function to call
        """
        self.mw.addonManager.setConfigAction(ADDON_MODULE, action)

    def _setupLocalHooks(self):
        self.mw.addonManager.setConfigUpdatedAction(
            ADDON_MODULE, lambda: self.save(storage="local"))

    # Local storage

    def _getLocal(self):
        """
        Read local storage config from disk

        Storage locations (add-on folder):
            - meta.json: user-specific
            - config.json: add-on defaults

        Anki 2.1: Managed by Anki.
        Anki 2.0: Managed by ConfigManager.

        Returns:
            dict -- Dictionary of config values
        """
        if ANKI21:
            return self.mw.addonManager.getConfig(ADDON_MODULE)
        else:
            config = self._addonConfigDefaults20()
            meta = self._addonMeta20()
            user_conf = meta.get("config", {})
            config.update(user_conf)
            return config

    def _getLocalDefaults(self):
        """
        Read default local storage config from disk

        Returns:
            dict -- Dictionary of default config values
        """
        if ANKI21:
            return self.mw.addonManager.addonConfigDefaults(ADDON_MODULE)
        else:
            return self._addonConfigDefaults20()

    def _saveLocal(self, config):
        """
        Save local storage config to disk

        Arguments:
            dict -- Dictionary of local config values
        """
        if ANKI21:
            self.mw.addonManager.writeConfig(ADDON_MODULE, config)
        else:
            self._writeAddonMeta20({"config": config})

    # Synced storage

    def _getSynced(self):
        """
        Read synced storage config from Anki collection object

        Returns:
            dict -- Dictionary of synced config values
        """
        return self._getStorageObj("synced")[self._conf_key]

    def _saveSynced(self, config):
        """
        Save synced storage config to Anki collection object

        Arguments:
            dict -- Dictionary of synced config values
        """
        self._getStorageObj("synced")[self._conf_key] = config

    # Profile storage

    def _getProfile(self):
        """
        Read profile storage config from Anki profile object

        Returns:
            dict -- Dictionary of profile config values
        """
        return self._getStorageObj("profile")[self._conf_key]

    def _saveProfile(self, config):
        """
        Save profile storage config to Anki profile object

        Arguments:
            dict -- Dictionary of profile config values
        """
        self._getStorageObj("profile")[self._conf_key] = config

    # Helper methods for synced & profile storage

    def _getStorageObj(self, name):
        """
        Get Anki storage dictionary for synced and profile storages.

        Storage objects:
            - synced: mw.col.conf
            - profile: mw.pm.profile

        Arguments:
            name {str} -- Name of config storage
                          ("synced" or "profile")

        Raises:
            NotImplementedError -- Config storage not supported
            ConfigError -- Config storage not ready, yet

        Returns:
            dict -- Anki storage dictionary
        """
        try:
            if name == "synced":
                storage_obj = self.mw.col.conf
            elif name == "profile":
                storage_obj = self.mw.pm.profile
            else:
                raise NotImplementedError(
                    "Storage object not implemented: ", name)
        except AttributeError:
            raise ConfigError("Config object is not ready, yet: ", name)

        if self._conf_key not in storage_obj:
            storage_obj[self._conf_key] = self._defaults[name]

        return storage_obj

    def _updateAnkiStorage(self):
        raise NotImplementedError()

    def _migrateProfileToLocal(self):
        self._migrateStorageToLocal("profile", self.mw.pm.profile)

    def _migrateSyncedToLocal(self):
        self._migrateStorageToLocal("synced", self.mw.col.conf)

    def _migrateStorageToLocal(self, name, storage_obj):
        raise NotImplementedError()

    # Helper methods for local storage on Anki 2.0

    def _addonMeta20(self):
        """Get meta dictionary

        Reads in meta.json in add-on folder and returns
        resulting dictionary of user-defined metadata values.

        Note:
            Anki 2.1 stores both add-on meta data and customized
            settings in meta.json. In this module we are only dealing
            with the settings part.

        Returns:
            dict: config dictionary

        """
        try:
            meta = json.load(
                io.open(DEFAULT_LOCAL_META_PATH, encoding="utf-8"))
        except (IOError, OSError):
            meta = None
        except ValueError as e:
            print("Could not read meta.json: " + str(e))
            meta = None

        if not meta:
            meta = {"config": self._addonConfigDefaults20()}
            self._writeAddonMeta20(meta)

        return meta

    def _writeAddonMeta20(self, meta):
        """Write meta dictionary

        Writes meta dictionary to meta.json in add-on folder.

        Args:
            meta (dict): meta dictionary

        """
        with io.open(DEFAULT_LOCAL_META_PATH, 'w', encoding="utf-8") as f:
            content = json.dumps(meta, indent=4, sort_keys=True,
                                 ensure_ascii=False)
            f.write(unicode(content))  # noqa: F821

    def _addonConfigDefaults20(self):
        """Get default config dictionary

        Reads in config.json in add-on folder and returns
        resulting dictionary of default config values.

        Returns:
            dict: config dictionary

        Raises:
            ConfigError: If config.json cannot be parsed correctly.
                (The assumption being that we would end up in an
                inconsistent state if we were to return an empty
                config dictionary. This should never happen.)

        """
        try:
            return json.load(io.open(DEFAULT_LOCAL_CONFIG_PATH,
                                     encoding="utf-8"))
        except (IOError, OSError, ValueError) as e:
            raise ConfigError("Config file could not be read: " + str(e))
