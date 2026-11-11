"""Tests for completer.locks: record locks that cannot go stale, the heartbeat and the unfinished-failures hint."""

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Completer"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from completer import locks  # noqa: E402
from completer.locks import Heartbeat, Locks, Unfinished  # noqa: E402

PACKAGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Completer")

# A tiny program for the "other process": take (or try to take) a lock byte, report, then wait on standard input.
HOLDER = r"""
import sys
sys.path.insert(0, sys.argv[1])
from completer.locks import Locks
mine = Locks(sys.argv[2])
got = mine.acquire(int(sys.argv[3]))
sys.stdout.write("held\n" if got else "busy\n")
sys.stdout.flush()
sys.stdin.readline()
"""

NEEDS_FCNTL = unittest.skipIf(locks.fcntl is None, "operating-system record locks are not available here")


class TempDirCase(unittest.TestCase):
    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.dir = holder.name


class LocksCase(TempDirCase):
    def locks(self, directory=None):
        mine = Locks(directory or self.dir)
        self.addCleanup(self.forget, mine)
        return mine

    def forget(self, mine):
        mine.release_all()
        descriptor = locks._files.pop(mine.path, None)
        if descriptor is not None:
            os.close(descriptor)

    def spawn(self, byte, directory=None):
        process = subprocess.Popen([sys.executable, "-c", HOLDER, PACKAGE_DIR, directory or self.dir, str(byte)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, universal_newlines=True)
        self.addCleanup(self.reap, process)
        return process

    @staticmethod
    def reap(process):
        if process.poll() is None:
            process.kill()
        process.wait(30)
        process.stdin.close()
        process.stdout.close()


class FolderTests(TempDirCase):
    def test_creates_the_directory(self):
        wanted = os.path.join(self.dir, "a", "b")
        self.assertEqual(locks.folder(wanted), wanted)
        self.assertTrue(os.path.isdir(wanted))

    def test_existing_directory_is_fine(self):
        self.assertEqual(locks.folder(self.dir), self.dir)
        self.assertEqual(locks.folder(self.dir), self.dir)

    def test_empty_means_system_temp_folder(self):
        self.assertEqual(locks.folder(""), tempfile.gettempdir())
        self.assertEqual(locks.folder(None), tempfile.gettempdir())

    def test_uncreatable_directory_does_not_raise(self):
        blocker = os.path.join(self.dir, "file")
        open(blocker, "w").close()
        self.assertEqual(locks.folder(os.path.join(blocker, "sub")), os.path.join(blocker, "sub"))


class LockTests(LocksCase):
    def test_acquire_and_release(self):
        mine = self.locks()
        self.assertTrue(mine.acquire(5))
        mine.release(5)
        self.assertTrue(mine.acquire(5))

    def test_lock_file_is_created_where_expected(self):
        mine = self.locks()
        mine.acquire(1)
        self.assertEqual(mine.path, os.path.join(self.dir, "completer-locks"))
        if locks.fcntl is not None:
            self.assertTrue(os.path.exists(mine.path))

    def test_same_byte_twice_by_one_object(self):
        mine = self.locks()
        self.assertTrue(mine.acquire(7))
        self.assertFalse(mine.acquire(7))

    def test_second_object_same_process_cannot_take_a_held_byte(self):
        first, second = self.locks(), self.locks()
        self.assertTrue(first.acquire(3))
        self.assertFalse(second.acquire(3))
        first.release(3)
        self.assertTrue(second.acquire(3))
        self.assertFalse(first.acquire(3))

    def test_bytes_are_independent(self):
        first, second = self.locks(), self.locks()
        self.assertTrue(first.acquire(locks.SWEEP))
        self.assertTrue(second.acquire(1234))
        self.assertFalse(second.acquire(locks.SWEEP))
        self.assertFalse(first.acquire(1234))
        first.release(locks.SWEEP)
        self.assertTrue(second.acquire(locks.SWEEP))
        self.assertFalse(first.acquire(1234))

    def test_sweep_is_byte_zero(self):
        self.assertEqual(locks.SWEEP, 0)

    def test_release_of_a_byte_not_held_is_a_no_op(self):
        first, second = self.locks(), self.locks()
        self.assertTrue(first.acquire(9))
        second.release(9)       # not second's lock: must not free it
        second.release(10)      # never taken at all
        self.assertFalse(second.acquire(9))
        first.release(9)
        first.release(9)        # twice is harmless
        self.assertTrue(second.acquire(9))

    def test_release_all(self):
        mine, other = self.locks(), self.locks()
        for byte in (0, 4, 8):
            self.assertTrue(mine.acquire(byte))
        mine.release_all()
        for byte in (0, 4, 8):
            self.assertTrue(other.acquire(byte), byte)

    def test_release_all_with_nothing_held(self):
        self.locks().release_all()

    def test_different_directories_do_not_interfere(self):
        other_dir = tempfile.TemporaryDirectory()
        self.addCleanup(other_dir.cleanup)
        first, second = self.locks(), self.locks(other_dir.name)
        self.assertTrue(first.acquire(2))
        self.assertTrue(second.acquire(2))

    @NEEDS_FCNTL
    def test_another_process_cannot_take_a_held_byte(self):
        mine = self.locks()
        self.assertTrue(mine.acquire(6))
        other = self.spawn(6)
        self.assertEqual(other.stdout.readline().strip(), "busy")

    @NEEDS_FCNTL
    def test_another_process_can_take_a_different_byte(self):
        mine = self.locks()
        self.assertTrue(mine.acquire(6))
        other = self.spawn(7)
        self.assertEqual(other.stdout.readline().strip(), "held")

    @NEEDS_FCNTL
    def test_we_cannot_take_a_byte_another_process_holds(self):
        other = self.spawn(11)
        self.assertEqual(other.stdout.readline().strip(), "held")
        mine = self.locks()
        self.assertFalse(mine.acquire(11))
        self.assertTrue(mine.acquire(12))
        # a failed attempt must not leave the byte marked as taken in this process
        other.stdin.write("\n")
        other.stdin.flush()
        other.wait(30)
        self.assertTrue(mine.acquire(11))

    @NEEDS_FCNTL
    def test_lock_is_free_again_after_the_holder_is_killed(self):
        other = self.spawn(13)
        self.assertEqual(other.stdout.readline().strip(), "held")
        mine = self.locks()
        self.assertFalse(mine.acquire(13))
        other.kill()
        other.wait(30)
        self.assertTrue(mine.acquire(13))

    @NEEDS_FCNTL
    def test_lock_is_free_again_after_the_holder_exits(self):
        other = self.spawn(14)
        self.assertEqual(other.stdout.readline().strip(), "held")
        other.stdin.write("\n")
        other.stdin.flush()
        other.wait(30)
        self.assertTrue(self.locks().acquire(14))

    @NEEDS_FCNTL
    def test_release_frees_the_byte_for_another_process(self):
        mine = self.locks()
        self.assertTrue(mine.acquire(15))
        mine.release(15)
        other = self.spawn(15)
        self.assertEqual(other.stdout.readline().strip(), "held")


class UnusableDirectoryTests(LocksCase):
    def blocked_directory(self):
        blocker = os.path.join(self.dir, "plain-file")
        open(blocker, "w").close()
        return os.path.join(blocker, "sub")  # a directory below a file: nothing can be created there

    def test_unusable_directory_fails_open(self):
        mine = self.locks(self.blocked_directory())
        self.assertTrue(mine.acquire(0))
        self.assertTrue(mine.acquire(5))

    def test_missing_directory_fails_open(self):
        mine = self.locks(os.path.join(self.dir, "does", "not", "exist"))
        self.assertTrue(mine.acquire(1))

    def test_release_without_a_lock_file_does_not_raise(self):
        mine = self.locks(self.blocked_directory())
        mine.acquire(2)
        mine.release(2)
        mine.release_all()

    def test_threads_still_exclude_each_other_without_a_file(self):
        directory = self.blocked_directory()
        first, second = self.locks(directory), self.locks(directory)
        self.assertTrue(first.acquire(4))
        self.assertFalse(second.acquire(4))
        first.release(4)
        self.assertTrue(second.acquire(4))


class HeartbeatTests(TempDirCase):
    def test_fresh_beat_is_alive(self):
        beat = Heartbeat(self.dir)
        self.assertTrue(beat.beat(1000))
        self.assertEqual(beat.age(1000), 0)
        self.assertTrue(beat.alive(1000))

    def test_path(self):
        self.assertEqual(Heartbeat(self.dir).path, os.path.join(self.dir, "completer-alive"))

    def test_899_seconds_alive_901_dead(self):
        beat = Heartbeat(self.dir)
        beat.beat(1000)
        self.assertEqual(beat.age(1899), 899)
        self.assertTrue(beat.alive(1899))
        self.assertTrue(beat.alive(1900))
        self.assertEqual(beat.age(1901), 901)
        self.assertFalse(beat.alive(1901))
        self.assertFalse(beat.alive(10 ** 9))

    def test_future_stamp_tolerated_up_to_300_seconds(self):
        beat = Heartbeat(self.dir)
        beat.beat(1000)
        self.assertTrue(beat.alive(900))
        self.assertTrue(beat.alive(700))
        self.assertFalse(beat.alive(699))
        self.assertFalse(beat.alive(0))

    def test_garbage_content(self):
        beat = Heartbeat(self.dir)
        for content in ("hello\n", "", "12abc", "1.5", "\x00\x01"):
            with open(beat.path, "w") as handle:
                handle.write(content)
            self.assertIsNone(beat.age(1000), repr(content))
            self.assertFalse(beat.alive(1000), repr(content))

    def test_binary_garbage_never_raises(self):
        beat = Heartbeat(self.dir)
        with open(beat.path, "wb") as handle:
            handle.write(b"\xff\xfe\xfd")
        try:
            self.assertFalse(beat.alive(1000))
        except UnicodeDecodeError:
            self.fail("an unreadable heartbeat file must count as not alive, not raise")

    def test_missing_file(self):
        beat = Heartbeat(self.dir)
        self.assertIsNone(beat.age(1000))
        self.assertFalse(beat.alive(1000))

    def test_missing_directory(self):
        beat = Heartbeat(os.path.join(self.dir, "nope"))
        self.assertIsNone(beat.age(1000))
        self.assertFalse(beat.alive(1000))

    def test_newer_beat_replaces_older(self):
        beat = Heartbeat(self.dir)
        beat.beat(1000)
        beat.beat(5000)
        self.assertEqual(beat.age(5000), 0)

    def test_atomic_replace_leaves_no_temporary_files(self):
        beat = Heartbeat(self.dir)
        for now in range(1000, 1010):
            self.assertTrue(beat.beat(now))
        self.assertEqual(os.listdir(self.dir), ["completer-alive"])

    def test_float_time_is_truncated(self):
        beat = Heartbeat(self.dir)
        beat.beat(1000.9)
        self.assertEqual(beat.age(1000), 0)

    def test_unwritable_directory_returns_false(self):
        beat = Heartbeat(os.path.join(self.dir, "nope"))
        self.assertFalse(beat.beat(1000))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "nope")))

    def test_directory_in_the_way_returns_false_and_cleans_up(self):
        beat = Heartbeat(self.dir)
        os.mkdir(beat.path)  # the stamp file cannot be replaced by a file
        self.assertFalse(beat.beat(1000))
        self.assertEqual(os.listdir(self.dir), ["completer-alive"])

    def test_life_constant(self):
        self.assertEqual(locks.HEARTBEAT_LIFE, 900)


class UnfinishedTests(TempDirCase):
    def test_path(self):
        self.assertEqual(Unfinished(self.dir).path, os.path.join(self.dir, "completer-unfinished"))

    def test_round_trip(self):
        hint = Unfinished(self.dir)
        self.assertEqual(hint.ids(), set())
        hint.add(12)
        hint.add(7)
        hint.add(12)
        self.assertEqual(hint.ids(), {7, 12})
        hint.remove(7)
        self.assertEqual(hint.ids(), {12})
        self.assertEqual(Unfinished(self.dir).ids(), {12})

    def test_removing_the_last_id_deletes_the_file(self):
        hint = Unfinished(self.dir)
        hint.add(3)
        self.assertTrue(os.path.exists(hint.path))
        hint.remove(3)
        self.assertFalse(os.path.exists(hint.path))
        self.assertEqual(os.listdir(self.dir), [])

    def test_removing_an_unknown_id_changes_nothing(self):
        hint = Unfinished(self.dir)
        hint.remove(5)
        self.assertEqual(os.listdir(self.dir), [])
        hint.add(1)
        hint.remove(5)
        self.assertEqual(hint.ids(), {1})

    def test_non_numeric_lines_ignored(self):
        hint = Unfinished(self.dir)
        with open(hint.path, "w") as handle:
            handle.write("5\nabc\n-3\n7\n1.5\n\n  8  \n0x10\n")
        self.assertEqual(hint.ids(), {5, 7, 8})

    def test_non_numeric_lines_dropped_on_the_next_write(self):
        hint = Unfinished(self.dir)
        with open(hint.path, "w") as handle:
            handle.write("5\nabc\n")
        hint.add(6)
        with open(hint.path) as handle:
            self.assertEqual(handle.read().split(), ["5", "6"])

    def test_at_most_200_ids_kept(self):
        hint = Unfinished(self.dir)
        hint._write(set(range(1, 501)))
        self.assertEqual(len(hint.ids()), 200)
        for number in range(1, 251):
            hint.add(number)
        self.assertEqual(len(hint.ids()), 200)
        self.assertEqual(hint.ids(), set(range(1, 201)))

    def test_atomic_replace_leaves_no_temporary_files(self):
        hint = Unfinished(self.dir)
        for number in range(10):
            hint.add(number + 1)
        self.assertEqual(os.listdir(self.dir), ["completer-unfinished"])

    def test_missing_directory_never_raises(self):
        hint = Unfinished(os.path.join(self.dir, "nope"))
        hint.add(1)
        hint.remove(1)
        self.assertEqual(hint.ids(), set())
        self.assertFalse(os.path.exists(os.path.join(self.dir, "nope")))

    def test_directory_in_the_way_never_raises(self):
        hint = Unfinished(self.dir)
        os.mkdir(hint.path)
        hint.add(1)
        hint.remove(1)
        self.assertEqual(hint.ids(), set())

    def test_binary_garbage_never_raises(self):
        hint = Unfinished(self.dir)
        with open(hint.path, "wb") as handle:
            handle.write(b"\xff\xfe\n12\n")
        try:
            hint.ids()
        except UnicodeDecodeError:
            self.fail("an unreadable hint file must read as empty, not raise")


if __name__ == "__main__":
    unittest.main()
