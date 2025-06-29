"""Tests for recorder."""
from __future__ import annotations

import datetime
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Literal
from unittest.mock import MagicMock, Mock, patch

import pytest
from sqlalchemy import insert
from sqlalchemy.orm import Session

from viseron.components.storage.models import Files, Recordings
from viseron.domains.camera import AbstractCamera
from viseron.domains.camera.recorder import (
    AbstractRecorder,
    RecorderBase,
    Recording,
    delete_recordings,
    get_recordings,
)
from viseron.watchdog.thread_watchdog import RestartableThread

from tests.common import MockCamera

if TYPE_CHECKING:
    from viseron import Viseron


@pytest.fixture(scope="function")
def get_db_session_recordings(get_db_session: Callable[[], Session]):
    """Fixture to test recordings with timezone edge cases."""
    with get_db_session() as session:
        # Create recordings across midnight UTC to test timezone handling
        session.execute(
            insert(Recordings).values(
                camera_identifier="test1",
                start_time=datetime.datetime(
                    2023, 3, 1, 23, 30, tzinfo=datetime.timezone.utc
                ),  # 23:30 UTC March 1
                adjusted_start_time=datetime.datetime(
                    2023, 3, 1, 23, 30, tzinfo=datetime.timezone.utc
                ),
                end_time=datetime.datetime(
                    2023, 3, 2, 0, 30, tzinfo=datetime.timezone.utc
                ),  # 00:30 UTC March 2
                thumbnail_path="test",
            )
        )
        # Mid-day recording
        session.execute(
            insert(Recordings).values(
                camera_identifier="test1",
                start_time=datetime.datetime(
                    2023, 3, 2, 12, 0, tzinfo=datetime.timezone.utc
                ),  # 12:00 UTC March 2
                adjusted_start_time=datetime.datetime(
                    2023, 3, 2, 12, 0, tzinfo=datetime.timezone.utc
                ),
                end_time=datetime.datetime(
                    2023, 3, 2, 13, 0, tzinfo=datetime.timezone.utc
                ),  # 13:00 UTC March 2
                thumbnail_path="test",
            )
        )
        # Recording near day boundary
        session.execute(
            insert(Recordings).values(
                camera_identifier="test1",
                start_time=datetime.datetime(
                    2023, 3, 2, 22, 45, tzinfo=datetime.timezone.utc
                ),  # 22:45 UTC March 2
                adjusted_start_time=datetime.datetime(
                    2023, 3, 2, 22, 45, tzinfo=datetime.timezone.utc
                ),
                end_time=datetime.datetime(
                    2023, 3, 2, 23, 45, tzinfo=datetime.timezone.utc
                ),  # 23:45 UTC March 2
                thumbnail_path="test",
            )
        )
        session.commit()
    yield get_db_session


def test_get_recordings_utc(get_db_session_recordings: Callable[[], Session]) -> None:
    """Test get_recordings in UTC."""
    recordings = get_recordings(
        get_db_session_recordings, "test1", utc_offset=datetime.timedelta(hours=0)
    )
    assert len(recordings) == 2  # March 1 and 2
    assert "2023-03-01" in recordings
    assert "2023-03-02" in recordings
    assert len(recordings["2023-03-02"]) == 2  # Two recordings on March 2 UTC


def test_get_recordings_positive_offset(
    get_db_session_recordings: Callable[[], Session]
) -> None:
    """Test get_recordings in UTC+5:30 (India)."""
    recordings = get_recordings(
        get_db_session_recordings,
        "test1",
        utc_offset=datetime.timedelta(hours=5, minutes=30),
    )
    # The 23:30 UTC March 1 recording should appear as 05:00 March 2 local time
    assert "2023-03-02" in recordings
    assert len(recordings["2023-03-02"]) == 2
    assert "2023-03-03" in recordings
    assert len(recordings["2023-03-03"]) == 1


def test_get_recordings_negative_offset(
    get_db_session_recordings: Callable[[], Session]
) -> None:
    """Test get_recordings in UTC-5 (Eastern)."""
    recordings = get_recordings(
        get_db_session_recordings, "test1", utc_offset=datetime.timedelta(hours=-5)
    )
    # The 23:30 UTC March 1 recording should appear as 18:30 March 1 local time
    assert "2023-03-01" in recordings
    assert len(recordings["2023-03-01"]) == 1


def test_get_recordings_date_specific_timezone(
    get_db_session_recordings: Callable[[], Session]
) -> None:
    """Test get_recordings with specific date in different timezone."""
    # Test in UTC+1
    recordings = get_recordings(
        get_db_session_recordings,
        "test1",
        utc_offset=datetime.timedelta(hours=1),
        date="2023-03-02",
    )
    # Should include recordings from 00:00 to 23:59 local time on March 2
    assert len(recordings) == 1
    assert "2023-03-02" in recordings

    # The 22:45 UTC recording should be 23:45 local time and still included
    recording_times = [rec["start_time"] for rec in recordings["2023-03-02"].values()]
    assert (
        datetime.datetime(2023, 3, 2, 22, 45, tzinfo=datetime.timezone.utc)
        in recording_times
    )


def test_get_recordings_latest_with_timezone(
    get_db_session_recordings: Callable[[], Session]
) -> None:
    """Test get_recordings latest flag with timezone consideration."""
    # Test in UTC-7 (PDT)
    recordings = get_recordings(
        get_db_session_recordings,
        "test1",
        utc_offset=datetime.timedelta(hours=-7),
        latest=True,
    )
    assert len(recordings) == 1
    # Latest recording (22:45 UTC) should appear as 15:45 local time
    latest_date = list(recordings.keys())[0]
    latest_recording = list(recordings[latest_date].values())[0]
    assert latest_recording["start_time"] == datetime.datetime(
        2023, 3, 2, 22, 45, tzinfo=datetime.timezone.utc
    )


def test_get_recordings_latest_daily_with_timezone(
    get_db_session_recordings: Callable[[], Session]
) -> None:
    """Test get_recordings latest daily flag with timezone consideration."""
    # Test in UTC+9 (Japan)
    recordings = get_recordings(
        get_db_session_recordings,
        "test1",
        utc_offset=datetime.timedelta(hours=9),
        latest=True,
        daily=True,
    )
    # Should get the latest recording for each day in the local timezone
    assert len(recordings) >= 1
    for _date, date_recordings in recordings.items():
        assert len(date_recordings) == 1  # Only one (latest) recording per day


def test_delete_recordings_with_timezone(
    get_db_session_recordings: Callable[[], Session]
) -> None:
    """Test delete_recordings with timezone consideration."""
    # Test deleting recordings for a specific date in UTC+1
    recordings = delete_recordings(
        get_db_session_recordings,
        "test1",
        date="2023-03-02",
        utc_offset=datetime.timedelta(hours=1),
    )
    # Should delete all recordings that fall within March 2 in UTC+1
    assert len(recordings) >= 1
    for recording in recordings:
        # Convert UTC time to local time and verify it falls within the specified date
        local_time = recording.start_time + datetime.timedelta(hours=1)
        assert local_time.date() == datetime.date(2023, 3, 2)


class Recorder(RecorderBase):
    """Recorder class."""

    @property
    def lookback(self) -> Literal[5]:
        """Return lookback."""
        return 5


class TestRecorderBase:
    """Test the RecorderBase class."""

    @patch("viseron.domains.camera.recorder.delete_recordings")
    def test_delete_recording(
        self,
        mock_delete_recording: Mock,
        vis: Viseron,
    ):
        """Test delete_recording."""
        mock_delete_recording.return_value = []
        recorder_base = Recorder(vis, MagicMock(), MockCamera())
        result = recorder_base.delete_recording(
            datetime.timedelta(seconds=time.localtime().tm_gmtoff)
        )
        assert result is False

        mock_delete_recording.return_value = [
            MagicMock(spec=Recordings),
            MagicMock(spec=Recordings),
        ]
        result = recorder_base.delete_recording(
            datetime.timedelta(seconds=time.localtime().tm_gmtoff)
        )
        assert result is True


#
# Test cases that work with segments
#
# Note: Segments are written to the database only after the file is fully written.
#       As a result, concatenation of segments cannot fully happen until the segments
#       within the recording time period have been completed.
#
#       Some of these test cases are designed to test this behavior.  They do this
#       by delaying the addition of segments so they are written after the recording
#       is ccomplete.
#


@pytest.mark.parametrize(
    "segments, expected",
    [
        ((-4), 1),
        ((-6, -3), 2),
        ((-20), 0),
    ],
)
class ConcreteTestRecorder(AbstractRecorder):
    """Test recorder class that implements abstract methods."""

    def __init__(self, vis: Viseron, config, camera: AbstractCamera) -> None:
        super().__init__(vis, "test", config, camera)

    def _start(self, recording, shared_frame, objects_in_fov) -> None:
        pass

    def _stop(self, recording) -> None:
        pass

    @property
    def lookback(self) -> Literal[5]:
        """Return lookback."""
        return 5


@pytest.fixture(name="add_recording_to_session")
def fixture_add_recording_to_session(
    get_db_session: Callable[[], Session]
) -> Callable[
    [int, datetime.datetime, datetime.datetime, datetime.datetime, str, str], None
]:
    """Fixture to add a recording to the session."""

    def _add_recording(
        recording_id: int,
        start_time: datetime.datetime,
        adjusted_start_time: datetime.datetime,
        end_time: datetime.datetime,
        camera_identifier: str,
        thumbnail_path: str,
    ) -> None:
        """Add a recording to the session."""
        with get_db_session() as session:
            session.execute(
                insert(Recordings).values(
                    id=recording_id,
                    camera_identifier=camera_identifier,
                    start_time=start_time,
                    adjusted_start_time=adjusted_start_time,
                    end_time=end_time,
                    thumbnail_path=thumbnail_path,
                )
            )
            session.commit()

    return _add_recording


@pytest.fixture(name="add_segment_to_session")
def fixture_add_segment_to_session(
    get_db_session: Callable[[], Session]
) -> Callable[[datetime.datetime, float, float], None]:
    """Fixture to add a segment to the session with an incrementing variable."""

    counter = {"value": 0}

    def _add_segment(
        segment_start: datetime.datetime, duration: float, delay: float
    ) -> None:
        """Add a segment to the session after a delay, incrementing counter."""
        counter["value"] += 1
        segment_number = counter["value"]

        time.sleep(delay)

        with get_db_session() as session:
            session.execute(
                insert(Files).values(
                    path=f"/tmp/fragment{segment_number}.mp4",
                    tier_id=1,
                    tier_path="/tmp/tier1",
                    camera_identifier="test1",
                    category="recorder",
                    subcategory="segments",
                    duration=duration,
                    directory="/tmp",
                    filename=f"fragment{segment_number}.mp4",
                    size=1024,
                    orig_ctime=segment_start,
                )
            )
            session.commit()

    return _add_segment


@pytest.fixture(name="recorder")
def fixture_patched_recorder(vis: Viseron):
    """Fixture to create a test recorder with mocked vis.add_entity."""
    with patch.object(vis, "add_entity") as mock_add_entity:
        config = MagicMock()
        nested_dict = {"filename_pattern": "%H-%M-%S"}
        config.__getitem__.return_value.__getitem__.side_effect = nested_dict.get
        recorder = ConcreteTestRecorder(vis, config, MockCamera())
        # pylint: disable=protected-access
        recorder._logger = MagicMock()
        assert mock_add_entity.call_count == 2

        yield recorder


@pytest.fixture(name="recording_params")
def fixture_recording_params():
    """Fixture to provide common recording parameters."""
    return {
        "record_id": 1,
        "start_time": datetime.datetime(
            2023, 3, 2, 12, 0, tzinfo=datetime.timezone.utc
        ),
        "adjusted_start_time": datetime.datetime(
            2023, 3, 2, 12, 0, tzinfo=datetime.timezone.utc
        ),
        "end_time": datetime.datetime(2023, 3, 2, 12, 0, tzinfo=datetime.timezone.utc)
        + datetime.timedelta(seconds=6.0),
        "camera_identifier": "test1",
        "thumbnail_path": "/tmp/thumbnail.jpg",
        "date": "2023-03-02",
    }


@pytest.fixture(name="create_recording")
def fixture_create_recording(recording_params):
    """Fixture to create a Recording object from params."""

    def _create_recording(**overrides):
        params = {**recording_params, **overrides}
        return Recording(
            id=params["record_id"],
            start_time=params["start_time"],
            start_timestamp=params["start_time"].timestamp(),
            end_time=params["end_time"],
            end_timestamp=params["end_time"].timestamp(),
            date=params.get("date", params["start_time"].strftime("%Y-%m-%d")),
            thumbnail=None,
            thumbnail_path=params["thumbnail_path"],
            clip_path=None,
            objects=[],
        )

    return _create_recording


@pytest.fixture(name="add_db_recording")
def fixture_add_db_recording(add_recording_to_session, recording_params):
    """Fixture to add a recording to the DB."""

    def _add_db_recording(**overrides):
        params = {**recording_params, **overrides}
        add_recording_to_session(
            params["record_id"],
            params["start_time"],
            params["adjusted_start_time"],
            params["end_time"],
            params["camera_identifier"],
            params["thumbnail_path"],
        )

    return _add_db_recording


class TestAbstractRecorder:
    """Test the AbstractRecorder class."""

    def test_no_active_recording(self, recorder: ConcreteTestRecorder):
        """Test no active recording."""
        recording = None

        recorder.stop(recording)

        # pylint: disable=protected-access
        recorder._logger.error.assert_called_with(  # type: ignore [attr-defined]
            "No active recording to stop"
        )
        assert recorder.active_recording is None

    @pytest.mark.skip(reason="to be replaced")
    def test_concatenate_fragments_no_fragments(
        self,
        get_db_session: Callable[[], Session],
        recorder: ConcreteTestRecorder,
        add_db_recording,
        create_recording,
        add_segment_to_session: Callable[[datetime.datetime, float, float], None],
    ):
        """Test when no fragments are available during recordingwindow."""
        add_db_recording()
        recording = create_recording()

        # pylint: disable=protected-access
        with patch.object(recorder._storage, "get_session") as mock_get_session, patch(
            "shutil.move"
        ) as _:  # unassigned patch is in case of test case failure
            mock_get_session.return_value = get_db_session()

            concat_thread = RestartableThread(
                name="viseron.camera.test.concatenate_fragments",
                target=recorder._concatenate_fragments,
                args=(recording,),
                register=False,
            )
            concat_thread.start()

            duration = 10

            # insert segment before recording+lookback and after recording
            before_recording = recording.start_time - datetime.timedelta(seconds=16)
            after_recording = (
                recording.start_time
                + datetime.timedelta(seconds=6.0)
                + datetime.timedelta(seconds=1)
            )
            add_segment_to_session(before_recording, duration, 0)
            add_segment_to_session(after_recording, duration, 0)

            concat_thread.join()

            # pylint: disable=protected-access
            recorder._logger.error.assert_called_with(  # type: ignore [attr-defined]
                "No fragments available."
            )
            assert recording.clip_path is None

    @pytest.mark.skip(reason="to be replaced")
    def test_segment_ends_during_recording(
        self,
        get_db_session: Callable[[], Session],
        recorder: ConcreteTestRecorder,
        add_db_recording,
        create_recording,
        add_segment_to_session: Callable[[datetime.datetime, float, float], None],
    ):
        """Fragment starts before the recording and ends during it."""
        add_db_recording()
        recording = create_recording()

        # pylint: disable=protected-access
        with patch.object(recorder._storage, "get_session") as mock_get_session, patch(
            "shutil.move"
        ) as mock_file_move:
            mock_get_session.return_value = get_db_session()

            concat_thread = RestartableThread(
                name="viseron.camera.test.concatenate_fragments",
                target=recorder._concatenate_fragments,
                args=(recording,),
                register=False,
            )
            concat_thread.start()

            recording_time = 6.0
            segment_duration = 10.0
            go_back = segment_duration - recording_time / 2
            segment_start = recording.start_time - datetime.timedelta(seconds=go_back)
            delay = segment_duration - go_back
            add_segment_to_session(segment_start, segment_duration, delay)

            concat_thread.join()

            assert mock_file_move.call_count == 1

        assert recording.clip_path is not None
        (date, filename) = recording.clip_path.split("/")[-2:]
        assert date == recording.date
        assert filename == f"{recording.start_time.strftime('%H-%M-%S')}.mp4"

    @pytest.mark.skip(reason="to be replaced")
    def test_recording_within_segment(
        self,
        get_db_session: Callable[[], Session],
        recorder: ConcreteTestRecorder,
        add_db_recording,
        create_recording,
        add_segment_to_session: Callable[[datetime.datetime, float, float], None],
    ):
        """Test _concatenate_fragments with a production failure case."""

        #
        # In this case a segment starts before beginning of recording and ends after
        # recording is complete.  Segments are not written until after the segment file
        # has been written to disk.
        #
        # This test case simulates this by delaying the addition of the segment
        # until after the recording end time.

        add_db_recording(
            adjusted_start_time=datetime.datetime(
                2023, 3, 2, 11, 59, 50, tzinfo=datetime.timezone.utc
            ),
            end_time=datetime.datetime(
                2023, 3, 2, 12, 0, 3, tzinfo=datetime.timezone.utc
            ),
        )
        recording = create_recording(
            adjusted_start_time=datetime.datetime(
                2023, 3, 2, 11, 59, 50, tzinfo=datetime.timezone.utc
            ),
            end_time=datetime.datetime(
                2023, 3, 2, 12, 0, 3, tzinfo=datetime.timezone.utc
            ),
        )

        # pylint: disable=protected-access
        with patch.object(recorder._storage, "get_session") as mock_get_session, patch(
            "shutil.move"
        ) as mock_file_move:
            mock_get_session.return_value = get_db_session()

            concat_thread = RestartableThread(
                name="viseron.camera.test.concatenate_fragments",
                target=recorder._concatenate_fragments,
                args=(recording,),
                register=False,
            )
            concat_thread.start()

            segment_duration = 10
            segment_start = recording.start_time - datetime.timedelta(seconds=6)
            t0 = (
                recording.start_time + datetime.timedelta(seconds=3)
            ).timestamp() - segment_start.timestamp()
            delay_before_insert = segment_duration - t0
            add_segment_to_session(segment_start, segment_duration, delay_before_insert)

            concat_thread.join()

            assert mock_file_move.call_count == 1

        assert recording.clip_path is not None
        (date, filename) = recording.clip_path.split("/")[-2:]
        assert date == recording.date
        assert filename == f"{recording.start_time.strftime('%H-%M-%S')}.mp4"

    @pytest.mark.parametrize(  # start of segment relative to start of recording
        "segments, expected",
        [
            # ([-2], 1),   temp copied out
            ([-7, 3], 2),
            # ([-20], 0),
        ],
    )
    def test_segment_cases(
        self,
        get_db_session: Callable[[], Session],
        recorder: ConcreteTestRecorder,
        add_db_recording,
        create_recording,
        add_segment_to_session: Callable[[datetime.datetime, float, float], None],
        segments,
        expected,
    ):
        """Fragment starts before the recording and ends during it."""
        # for segment in segments:
        #     print(f"{segment=}")
        add_db_recording()
        recording = create_recording()

        # pylint: disable=protected-access
        with patch.object(recorder._storage, "get_session") as mock_get_session, patch(
            "shutil.move"
        ) as mock_file_move:
            mock_get_session.return_value = get_db_session()

            concat_thread = RestartableThread(
                name="viseron.camera.test.concatenate_fragments",
                target=recorder._concatenate_fragments,
                args=(recording,),
                register=False,
            )
            concat_thread.start()

            recording_time = 6.0  # replace with calculation or common variable
            segment_duration = 10.0  # segment length
            for segment in segments:
                # go_back = segment_duration - recording_time / 2
                #             - D -
                #             |   |
                #            SD  ED
                #   SS   SR--ER  ES
                #   |             |
                #   |- S Length --|
                # negative values of D are zero delay (segment written by ER)

                # RT = recording time
                # D = S Length - RT + SS
                # if D < 0 D = 0
                # 10 - 6 + -2 = +2
                delay = segment_duration - recording_time + segment
                delay = max(delay, 0.0)
                print(f"{delay=}")
                segment_start = recording.start_time + datetime.timedelta(
                    seconds=segment
                )
                # delay = segment_duration - go_back
                add_segment_to_session(segment_start, segment_duration, delay)

            print(
                f"Recording start: {recording.start_time}, "
                f"Recording end: {recording.end_time}"
            )
            with get_db_session() as session:
                files = session.query(Files).all()
                print("Files records:")
                for file in files:
                    print(
                        f"File: {file.filename}, "
                        f"Start: {file.orig_ctime}, "
                        f"Duration: {file.duration}s, "
                        f"Size: {file.size} bytes, "
                        f"Path: {file.path}"
                    )
            concat_thread.join()

            assert mock_file_move.call_count == expected  # CHANGE: Move done only once!

        assert recording.clip_path is not None
        (date, filename) = recording.clip_path.split("/")[-2:]
        assert date == recording.date
        assert filename == f"{recording.start_time.strftime('%H-%M-%S')}.mp4"
