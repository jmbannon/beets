# This file is part of beets.
# Copyright 2016, Thomas Scholtes.
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.


import itertools
import os
import shutil
import unittest
from test import helper
from test._common import RSRC
from test.test_importer import AutotagStub, ImportHelper
from test.test_ui_importer import TerminalImportSessionSetup
from unittest.mock import ANY, Mock, patch

from mediafile import MediaFile

from beets import config, plugins, ui
from beets.dbcore import types
from beets.importer import (
    ArchiveImportTask,
    SentinelImportTask,
    SingletonImportTask,
    action,
)
from beets.library import Item
from beets.plugins import MetadataSourcePlugin
from beets.util import bytestring_path, displayable_path, syspath
from beets.util.id_extractors import (
    beatport_id_regex,
    deezer_id_regex,
    spotify_id_regex,
)


class TestHelper(helper.TestHelper):
    def setup_plugin_loader(self):
        # FIXME the mocking code is horrific, but this is the lowest and
        # earliest level of the plugin mechanism we can hook into.
        self.load_plugins()
        self._plugin_loader_patch = patch("beets.plugins.load_plugins")
        self._plugin_classes = set()
        load_plugins = self._plugin_loader_patch.start()

        def myload(names=()):
            plugins._classes.update(self._plugin_classes)

        load_plugins.side_effect = myload
        self.setup_beets()

    def teardown_plugin_loader(self):
        self._plugin_loader_patch.stop()
        self.unload_plugins()

    def register_plugin(self, plugin_class):
        self._plugin_classes.add(plugin_class)


class ItemTypesTest(unittest.TestCase, TestHelper):
    def setUp(self):
        self.setup_plugin_loader()

    def tearDown(self):
        self.teardown_plugin_loader()
        self.teardown_beets()

    def test_flex_field_type(self):
        class RatingPlugin(plugins.BeetsPlugin):
            item_types = {"rating": types.Float()}

        self.register_plugin(RatingPlugin)
        self.config["plugins"] = "rating"

        item = Item(path="apath", artist="aaa")
        item.add(self.lib)

        # Do not match unset values
        out = self.run_with_output("ls", "rating:1..3")
        self.assertNotIn("aaa", out)

        self.run_command("modify", "rating=2", "--yes")

        # Match in range
        out = self.run_with_output("ls", "rating:1..3")
        self.assertIn("aaa", out)

        # Don't match out of range
        out = self.run_with_output("ls", "rating:3..5")
        self.assertNotIn("aaa", out)


class ItemWriteTest(unittest.TestCase, TestHelper):
    def setUp(self):
        self.setup_plugin_loader()
        self.setup_beets()

        class EventListenerPlugin(plugins.BeetsPlugin):
            pass

        self.event_listener_plugin = EventListenerPlugin()
        self.register_plugin(EventListenerPlugin)

    def tearDown(self):
        self.teardown_plugin_loader()
        self.teardown_beets()

    def test_change_tags(self):
        def on_write(item=None, path=None, tags=None):
            if tags["artist"] == "XXX":
                tags["artist"] = "YYY"

        self.register_listener("write", on_write)

        item = self.add_item_fixture(artist="XXX")
        item.write()

        mediafile = MediaFile(syspath(item.path))
        self.assertEqual(mediafile.artist, "YYY")

    def register_listener(self, event, func):
        self.event_listener_plugin.register_listener(event, func)


class ItemTypeConflictTest(unittest.TestCase, TestHelper):
    def setUp(self):
        self.setup_plugin_loader()
        self.setup_beets()

    def tearDown(self):
        self.teardown_plugin_loader()
        self.teardown_beets()

    def test_mismatch(self):
        class EventListenerPlugin(plugins.BeetsPlugin):
            item_types = {"duplicate": types.INTEGER}

        class AdventListenerPlugin(plugins.BeetsPlugin):
            item_types = {"duplicate": types.FLOAT}

        self.event_listener_plugin = EventListenerPlugin
        self.advent_listener_plugin = AdventListenerPlugin
        self.register_plugin(EventListenerPlugin)
        self.register_plugin(AdventListenerPlugin)
        self.assertRaises(plugins.PluginConflictException, plugins.types, Item)

    def test_match(self):
        class EventListenerPlugin(plugins.BeetsPlugin):
            item_types = {"duplicate": types.INTEGER}

        class AdventListenerPlugin(plugins.BeetsPlugin):
            item_types = {"duplicate": types.INTEGER}

        self.event_listener_plugin = EventListenerPlugin
        self.advent_listener_plugin = AdventListenerPlugin
        self.register_plugin(EventListenerPlugin)
        self.register_plugin(AdventListenerPlugin)
        self.assertNotEqual(None, plugins.types(Item))


class EventsTest(unittest.TestCase, ImportHelper, TestHelper):
    def setUp(self):
        self.setup_plugin_loader()
        self.setup_beets()
        self.__create_import_dir(2)
        config["import"]["pretend"] = True

    def tearDown(self):
        self.teardown_plugin_loader()
        self.teardown_beets()

    def __copy_file(self, dest_path, metadata):
        # Copy files
        resource_path = os.path.join(RSRC, b"full.mp3")
        shutil.copy(syspath(resource_path), syspath(dest_path))
        medium = MediaFile(dest_path)
        # Set metadata
        for attr in metadata:
            setattr(medium, attr, metadata[attr])
        medium.save()

    def __create_import_dir(self, count):
        self.import_dir = os.path.join(self.temp_dir, b"testsrcdir")
        if os.path.isdir(syspath(self.import_dir)):
            shutil.rmtree(syspath(self.import_dir))

        self.album_path = os.path.join(self.import_dir, b"album")
        os.makedirs(self.album_path)

        metadata = {
            "artist": "Tag Artist",
            "album": "Tag Album",
            "albumartist": None,
            "mb_trackid": None,
            "mb_albumid": None,
            "comp": None,
        }
        self.file_paths = []
        for i in range(count):
            metadata["track"] = i + 1
            metadata["title"] = "Tag Title Album %d" % (i + 1)
            track_file = bytestring_path("%02d - track.mp3" % (i + 1))
            dest_path = os.path.join(self.album_path, track_file)
            self.__copy_file(dest_path, metadata)
            self.file_paths.append(dest_path)

    def test_import_task_created(self):
        import_files = [self.import_dir]
        self._setup_import_session(singletons=False)
        self.importer.paths = import_files

        with helper.capture_log() as logs:
            self.importer.run()
        self.unload_plugins()

        # Exactly one event should have been imported (for the album).
        # Sentinels do not get emitted.
        self.assertEqual(logs.count("Sending event: import_task_created"), 1)

        logs = [line for line in logs if not line.startswith("Sending event:")]
        self.assertEqual(
            logs,
            [
                "Album: {}".format(
                    displayable_path(os.path.join(self.import_dir, b"album"))
                ),
                "  {}".format(displayable_path(self.file_paths[0])),
                "  {}".format(displayable_path(self.file_paths[1])),
            ],
        )

    def test_import_task_created_with_plugin(self):
        class ToSingletonPlugin(plugins.BeetsPlugin):
            def __init__(self):
                super().__init__()

                self.register_listener(
                    "import_task_created", self.import_task_created_event
                )

            def import_task_created_event(self, session, task):
                if (
                    isinstance(task, SingletonImportTask)
                    or isinstance(task, SentinelImportTask)
                    or isinstance(task, ArchiveImportTask)
                ):
                    return task

                new_tasks = []
                for item in task.items:
                    new_tasks.append(SingletonImportTask(task.toppath, item))

                return new_tasks

        to_singleton_plugin = ToSingletonPlugin
        self.register_plugin(to_singleton_plugin)

        import_files = [self.import_dir]
        self._setup_import_session(singletons=False)
        self.importer.paths = import_files

        with helper.capture_log() as logs:
            self.importer.run()
        self.unload_plugins()

        # Exactly one event should have been imported (for the album).
        # Sentinels do not get emitted.
        self.assertEqual(logs.count("Sending event: import_task_created"), 1)

        logs = [line for line in logs if not line.startswith("Sending event:")]
        self.assertEqual(
            logs,
            [
                "Singleton: {}".format(displayable_path(self.file_paths[0])),
                "Singleton: {}".format(displayable_path(self.file_paths[1])),
            ],
        )


class HelpersTest(unittest.TestCase):
    def test_sanitize_choices(self):
        self.assertEqual(
            plugins.sanitize_choices(["A", "Z"], ("A", "B")), ["A"]
        )
        self.assertEqual(plugins.sanitize_choices(["A", "A"], ("A")), ["A"])
        self.assertEqual(
            plugins.sanitize_choices(["D", "*", "A"], ("A", "B", "C", "D")),
            ["D", "B", "C", "A"],
        )


class ListenersTest(unittest.TestCase, TestHelper):
    def setUp(self):
        self.setup_plugin_loader()

    def tearDown(self):
        self.teardown_plugin_loader()
        self.teardown_beets()

    def test_register(self):
        class DummyPlugin(plugins.BeetsPlugin):
            def __init__(self):
                super().__init__()
                self.register_listener("cli_exit", self.dummy)
                self.register_listener("cli_exit", self.dummy)

            def dummy(self):
                pass

        d = DummyPlugin()
        self.assertEqual(DummyPlugin._raw_listeners["cli_exit"], [d.dummy])

        d2 = DummyPlugin()
        self.assertEqual(
            DummyPlugin._raw_listeners["cli_exit"], [d.dummy, d2.dummy]
        )

        d.register_listener("cli_exit", d2.dummy)
        self.assertEqual(
            DummyPlugin._raw_listeners["cli_exit"], [d.dummy, d2.dummy]
        )

    @patch("beets.plugins.find_plugins")
    @patch("inspect.getfullargspec")
    def test_events_called(self, mock_gfa, mock_find_plugins):
        mock_gfa.return_value = Mock(
            args=(),
            varargs="args",
            varkw="kwargs",
        )

        class DummyPlugin(plugins.BeetsPlugin):
            def __init__(self):
                super().__init__()
                self.foo = Mock(__name__="foo")
                self.register_listener("event_foo", self.foo)
                self.bar = Mock(__name__="bar")
                self.register_listener("event_bar", self.bar)

        d = DummyPlugin()
        mock_find_plugins.return_value = (d,)

        plugins.send("event")
        d.foo.assert_has_calls([])
        d.bar.assert_has_calls([])

        plugins.send("event_foo", var="tagada")
        d.foo.assert_called_once_with(var="tagada")
        d.bar.assert_has_calls([])

    @patch("beets.plugins.find_plugins")
    def test_listener_params(self, mock_find_plugins):
        test = self

        class DummyPlugin(plugins.BeetsPlugin):
            def __init__(self):
                super().__init__()
                for i in itertools.count(1):
                    try:
                        meth = getattr(self, f"dummy{i}")
                    except AttributeError:
                        break
                    self.register_listener(f"event{i}", meth)

            def dummy1(self, foo):
                test.assertEqual(foo, 5)

            def dummy2(self, foo=None):
                test.assertEqual(foo, 5)

            def dummy3(self):
                # argument cut off
                pass

            def dummy4(self, bar=None):
                # argument cut off
                pass

            def dummy5(self, bar):
                test.assertFalse(True)

            # more complex examples

            def dummy6(self, foo, bar=None):
                test.assertEqual(foo, 5)
                test.assertEqual(bar, None)

            def dummy7(self, foo, **kwargs):
                test.assertEqual(foo, 5)
                test.assertEqual(kwargs, {})

            def dummy8(self, foo, bar, **kwargs):
                test.assertFalse(True)

            def dummy9(self, **kwargs):
                test.assertEqual(kwargs, {"foo": 5})

        d = DummyPlugin()
        mock_find_plugins.return_value = (d,)

        plugins.send("event1", foo=5)
        plugins.send("event2", foo=5)
        plugins.send("event3", foo=5)
        plugins.send("event4", foo=5)

        with self.assertRaises(TypeError):
            plugins.send("event5", foo=5)

        plugins.send("event6", foo=5)
        plugins.send("event7", foo=5)

        with self.assertRaises(TypeError):
            plugins.send("event8", foo=5)

        plugins.send("event9", foo=5)


class PromptChoicesTest(
    TerminalImportSessionSetup, unittest.TestCase, ImportHelper, TestHelper
):
    def setUp(self):
        self.setup_plugin_loader()
        self.setup_beets()
        self._create_import_dir(3)
        self._setup_import_session()
        self.matcher = AutotagStub().install()
        # keep track of ui.input_option() calls
        self.input_options_patcher = patch(
            "beets.ui.input_options", side_effect=ui.input_options
        )
        self.mock_input_options = self.input_options_patcher.start()

    def tearDown(self):
        self.input_options_patcher.stop()
        self.teardown_plugin_loader()
        self.teardown_beets()
        self.matcher.restore()

    def test_plugin_choices_in_ui_input_options_album(self):
        """Test the presence of plugin choices on the prompt (album)."""

        class DummyPlugin(plugins.BeetsPlugin):
            def __init__(self):
                super().__init__()
                self.register_listener(
                    "before_choose_candidate", self.return_choices
                )

            def return_choices(self, session, task):
                return [
                    ui.commands.PromptChoice("f", "Foo", None),
                    ui.commands.PromptChoice("r", "baR", None),
                ]

        self.register_plugin(DummyPlugin)
        # Default options + extra choices by the plugin ('Foo', 'Bar')
        opts = (
            "Apply",
            "More candidates",
            "Skip",
            "Use as-is",
            "as Tracks",
            "Group albums",
            "Enter search",
            "enter Id",
            "aBort",
        ) + ("Foo", "baR")

        self.importer.add_choice(action.SKIP)
        self.importer.run()
        self.mock_input_options.assert_called_once_with(
            opts, default="a", require=ANY
        )

    def test_plugin_choices_in_ui_input_options_singleton(self):
        """Test the presence of plugin choices on the prompt (singleton)."""

        class DummyPlugin(plugins.BeetsPlugin):
            def __init__(self):
                super().__init__()
                self.register_listener(
                    "before_choose_candidate", self.return_choices
                )

            def return_choices(self, session, task):
                return [
                    ui.commands.PromptChoice("f", "Foo", None),
                    ui.commands.PromptChoice("r", "baR", None),
                ]

        self.register_plugin(DummyPlugin)
        # Default options + extra choices by the plugin ('Foo', 'Bar')
        opts = (
            "Apply",
            "More candidates",
            "Skip",
            "Use as-is",
            "Enter search",
            "enter Id",
            "aBort",
        ) + ("Foo", "baR")

        config["import"]["singletons"] = True
        self.importer.add_choice(action.SKIP)
        self.importer.run()
        self.mock_input_options.assert_called_with(
            opts, default="a", require=ANY
        )

    def test_choices_conflicts(self):
        """Test the short letter conflict solving."""

        class DummyPlugin(plugins.BeetsPlugin):
            def __init__(self):
                super().__init__()
                self.register_listener(
                    "before_choose_candidate", self.return_choices
                )

            def return_choices(self, session, task):
                return [
                    ui.commands.PromptChoice("a", "A foo", None),  # dupe
                    ui.commands.PromptChoice("z", "baZ", None),  # ok
                    ui.commands.PromptChoice("z", "Zupe", None),  # dupe
                    ui.commands.PromptChoice("z", "Zoo", None),
                ]  # dupe

        self.register_plugin(DummyPlugin)
        # Default options + not dupe extra choices by the plugin ('baZ')
        opts = (
            "Apply",
            "More candidates",
            "Skip",
            "Use as-is",
            "as Tracks",
            "Group albums",
            "Enter search",
            "enter Id",
            "aBort",
        ) + ("baZ",)
        self.importer.add_choice(action.SKIP)
        self.importer.run()
        self.mock_input_options.assert_called_once_with(
            opts, default="a", require=ANY
        )

    def test_plugin_callback(self):
        """Test that plugin callbacks are being called upon user choice."""

        class DummyPlugin(plugins.BeetsPlugin):
            def __init__(self):
                super().__init__()
                self.register_listener(
                    "before_choose_candidate", self.return_choices
                )

            def return_choices(self, session, task):
                return [ui.commands.PromptChoice("f", "Foo", self.foo)]

            def foo(self, session, task):
                pass

        self.register_plugin(DummyPlugin)
        # Default options + extra choices by the plugin ('Foo', 'Bar')
        opts = (
            "Apply",
            "More candidates",
            "Skip",
            "Use as-is",
            "as Tracks",
            "Group albums",
            "Enter search",
            "enter Id",
            "aBort",
        ) + ("Foo",)

        # DummyPlugin.foo() should be called once
        with patch.object(DummyPlugin, "foo", autospec=True) as mock_foo:
            with helper.control_stdin("\n".join(["f", "s"])):
                self.importer.run()
            self.assertEqual(mock_foo.call_count, 1)

        # input_options should be called twice, as foo() returns None
        self.assertEqual(self.mock_input_options.call_count, 2)
        self.mock_input_options.assert_called_with(
            opts, default="a", require=ANY
        )

    def test_plugin_callback_return(self):
        """Test that plugin callbacks that return a value exit the loop."""

        class DummyPlugin(plugins.BeetsPlugin):
            def __init__(self):
                super().__init__()
                self.register_listener(
                    "before_choose_candidate", self.return_choices
                )

            def return_choices(self, session, task):
                return [ui.commands.PromptChoice("f", "Foo", self.foo)]

            def foo(self, session, task):
                return action.SKIP

        self.register_plugin(DummyPlugin)
        # Default options + extra choices by the plugin ('Foo', 'Bar')
        opts = (
            "Apply",
            "More candidates",
            "Skip",
            "Use as-is",
            "as Tracks",
            "Group albums",
            "Enter search",
            "enter Id",
            "aBort",
        ) + ("Foo",)

        # DummyPlugin.foo() should be called once
        with helper.control_stdin("f\n"):
            self.importer.run()

        # input_options should be called once, as foo() returns SKIP
        self.mock_input_options.assert_called_once_with(
            opts, default="a", require=ANY
        )


class ParseSpotifyIDTest(unittest.TestCase):
    def test_parse_id_correct(self):
        id_string = "39WqpoPgZxygo6YQjehLJJ"
        out = MetadataSourcePlugin._get_id("album", id_string, spotify_id_regex)
        self.assertEqual(out, id_string)

    def test_parse_id_non_id_returns_none(self):
        id_string = "blah blah"
        out = MetadataSourcePlugin._get_id("album", id_string, spotify_id_regex)
        self.assertEqual(out, None)

    def test_parse_id_url_finds_id(self):
        id_string = "39WqpoPgZxygo6YQjehLJJ"
        id_url = "https://open.spotify.com/album/%s" % id_string
        out = MetadataSourcePlugin._get_id("album", id_url, spotify_id_regex)
        self.assertEqual(out, id_string)


class ParseDeezerIDTest(unittest.TestCase):
    def test_parse_id_correct(self):
        id_string = "176356382"
        out = MetadataSourcePlugin._get_id("album", id_string, deezer_id_regex)
        self.assertEqual(out, id_string)

    def test_parse_id_non_id_returns_none(self):
        id_string = "blah blah"
        out = MetadataSourcePlugin._get_id("album", id_string, deezer_id_regex)
        self.assertEqual(out, None)

    def test_parse_id_url_finds_id(self):
        id_string = "176356382"
        id_url = "https://www.deezer.com/album/%s" % id_string
        out = MetadataSourcePlugin._get_id("album", id_url, deezer_id_regex)
        self.assertEqual(out, id_string)


class ParseBeatportIDTest(unittest.TestCase):
    def test_parse_id_correct(self):
        id_string = "3089651"
        out = MetadataSourcePlugin._get_id(
            "album", id_string, beatport_id_regex
        )
        self.assertEqual(out, id_string)

    def test_parse_id_non_id_returns_none(self):
        id_string = "blah blah"
        out = MetadataSourcePlugin._get_id(
            "album", id_string, beatport_id_regex
        )
        self.assertEqual(out, None)

    def test_parse_id_url_finds_id(self):
        id_string = "3089651"
        id_url = "https://www.beatport.com/release/album-name/%s" % id_string
        out = MetadataSourcePlugin._get_id("album", id_url, beatport_id_regex)
        self.assertEqual(out, id_string)


def suite():
    return unittest.TestLoader().loadTestsFromName(__name__)


if __name__ == "__main__":
    unittest.main(defaultTest="suite")
