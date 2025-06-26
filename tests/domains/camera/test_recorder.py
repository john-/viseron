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
#       As a result, concatenation of segments cannot fully happen unto the segments
#       within the recording time period have been completed.
#
#       Yhese test cases are designed to test this behavior.  They do this
#       by delaying the addition of segments so they are written after the recording
#       is ccomplete.
#


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


# @pytest.fixture(name="session_with_recording")
# def fixture_session_with_recording(get_db_session: Callable[[], Session]):
#     """Fixture to test fragments."""

#     # 12:00 UTC March 2
#     base_time = datetime.datetime(2023, 3, 2, 12, 0, tzinfo=datetime.timezone.utc)
#     recording_start = base_time
#     recording_end = recording_start + datetime.timedelta(seconds=6.0)
#     with get_db_session() as session:
#         session.execute(
#             insert(Recordings).values(
#                 id=1,
#                 camera_identifier="test1",
#                 start_time=recording_start,
#                 adjusted_start_time=recording_start,
#                 end_time=recording_end,
#                 thumbnail_path="test",
#             )
#         )
#         session.commit()
#     yield get_db_session


# @pytest.fixture(name="db_session_no_recordings")
# def fixture_db_session_no_recordings(
#     get_db_session: Callable[[], Session]
# ) -> Callable[[], Session]:
#     """Fixture to provide a database session for tests."""

#     def _get_db_session() -> Session:
#         """Return a new database session."""
#         return get_db_session()

#     return _get_db_session


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


# to be REPLACED
# @pytest.fixture(name="get_db_session_fragments")
# def fixture_get_db_session_fragments(get_db_session: Callable[[], Session]):
#     """Fixture to test fragments."""

#     # *Note:* This only works if the fragments have been written first which
# is not the case
#     # unless the concatenation has the delay

#     # this is the case where the recording is between the start and
#     # end of the file segment
#     # 12:00 UTC March 2
#     base_time = datetime.datetime(2023, 3, 2, 12, 0, tzinfo=datetime.timezone.utc)
#     recording_start = base_time
#     recording_end = recording_start + datetime.timedelta(seconds=2.0)
#     # segment_start = recording_start - datetime.timedelta(seconds=6.0)
#     # segment_duration = 10.0
#     with get_db_session() as session:
#         session.execute(
#             insert(Recordings).values(
#                 camera_identifier="test1",
#                 start_time=recording_start,
#                 adjusted_start_time=recording_start,
#                 end_time=recording_end,
#                 thumbnail_path="test",
#             )
#         )
#         # session.execute(
#         #     insert(Files).values(
#         #         path="/tmp/fragment1.mp4",
#         #         tier_id=1,
#         #         tier_path="/tmp/tier1",
#         #         camera_identifier="test1",
#         #         category="recorder",
#         #         subcategory="segments",
#         #         duration=segment_duration,
#         #         directory="/tmp",
#         #         filename="fragment1.mp4",
#         #         size=1024,
#         #         orig_ctime=segment_start,
#         #     )
#         # )
#         session.commit()
#     yield get_db_session


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

    # @pytest.mark.skip(reason="Skipping for a bit")
    def test_concatenate_fragments_no_fragments(
        self,
        get_db_session: Callable[[], Session],
        recorder: ConcreteTestRecorder,
        add_recording_to_session: Callable[
            [int, datetime.datetime, datetime.datetime, datetime.datetime, str, str],
            None,
        ],
        add_segment_to_session: Callable[[datetime.datetime, float, float], None],
    ):
        """Test when no fragments are available during recordingwindow."""
        record_id = 1
        start_time = datetime.datetime(2023, 3, 2, 12, 0, tzinfo=datetime.timezone.utc)
        adjusted_start_time = start_time
        end_time = start_time + datetime.timedelta(seconds=6.0)
        camera_identifier = "test1"
        thumbnail_path = "/tmp/thumbnail.jpg"
        add_recording_to_session(
            record_id,
            start_time,
            adjusted_start_time,
            end_time,
            camera_identifier,
            thumbnail_path,
        )

        recording = Recording(
            id=record_id,
            start_time=start_time,
            start_timestamp=start_time.timestamp(),
            end_time=None,
            end_timestamp=None,
            date="2023-03-02",
            thumbnail=None,
            thumbnail_path=thumbnail_path,
            clip_path=None,
            objects=[],
        )

        # pylint: disable=protected-access
        with patch.object(recorder._storage, "get_session") as mock_get_session:
            # Return a list of fragments from the database session
            mock_get_session.return_value = get_db_session()

            concat_thread = RestartableThread(
                name="viseron.camera.test.concatenate_fragments",
                target=recorder._concatenate_fragments,
                args=(recording,),
                register=False,
            )
            concat_thread.start()

            segments = [
                {
                    "start": start_time
                    - datetime.timedelta(seconds=16),  # includes lookback
                    "duration": 10,
                },
                {
                    "start": end_time
                    + datetime.timedelta(seconds=1),  # 1 second past end of recording
                    "duration": 10,
                },
            ]

            for segment in segments:
                print(segment)

            for segment in segments:
                add_segment_to_session(segment["start"], segment["duration"], 0)

            concat_thread.join()

            # pylint: disable=protected-access
            recorder._logger.error.assert_called_with(  # type: ignore [attr-defined]
                "No fragments available."
            )
            assert recording.clip_path is None

    @pytest.mark.skip(reason="Skipping for a bit")
    def test_fragments_in_progress_during_recording(
        self,
        get_db_session: Callable[[], Session],
        recorder: ConcreteTestRecorder,
        add_recording_to_session: Callable[
            [int, datetime.datetime, datetime.datetime, datetime.datetime, str, str],
            None,
        ],
        add_segment_to_session: Callable[[datetime.datetime, float, float], None],
    ):
        """Fragment starts before the recording and ends during it."""
        record_id = 1
        start_time = datetime.datetime(2023, 3, 2, 12, 0, tzinfo=datetime.timezone.utc)
        adjusted_start_time = start_time
        end_time = start_time + datetime.timedelta(seconds=6.0)
        camera_identifier = "test1"
        thumbnail_path = "/tmp/thumbnail.jpg"
        add_recording_to_session(
            record_id,
            start_time,
            adjusted_start_time,
            end_time,
            camera_identifier,
            thumbnail_path,
        )

        recording = Recording(
            id=record_id,
            start_time=start_time,
            start_timestamp=start_time.timestamp(),  # is this needed?
            end_time=None,
            end_timestamp=None,
            date="2023-03-02",
            thumbnail=None,
            thumbnail_path=thumbnail_path,
            clip_path=None,
            objects=[],
        )

        # pylint: disable=protected-access
        with patch.object(recorder._storage, "get_session") as mock_get_session, patch(
            "shutil.move"
        ) as mock_file_move:
            # Return a list of fragments from the database session
            mock_get_session.return_value = get_db_session()

            # recorder runs concatenate_fragments in a thread
            concat_thread = RestartableThread(
                name="viseron.camera.test.concatenate_fragments",
                target=recorder._concatenate_fragments,
                args=(recording,),
                register=False,
            )
            concat_thread.start()

            # while concatenation is running insert a segment
            recording_time = 6.0
            segment_duration = 10.0
            # simulate completing starting before recording and completing in the
            # middle of it
            go_back = segment_duration - recording_time / 2
            segment_start = start_time - datetime.timedelta(seconds=go_back)
            delay = segment_duration - go_back
            add_segment_to_session(segment_start, segment_duration, delay)

            concat_thread.join()

            assert mock_file_move.call_count == 1

        assert recording.clip_path is not None
        (date, filename) = recording.clip_path.split("/")[-2:]
        assert date == recording.date
        assert filename == f"{recording.start_time.strftime('%H-%M-%S')}.mp4"

    @pytest.mark.skip(reason="Skipping for a bit")
    def test_prod_failure_case(
        self,
        get_db_session: Callable[[], Session],
        recorder: ConcreteTestRecorder,
        add_recording_to_session: Callable[
            [int, datetime.datetime, datetime.datetime, datetime.datetime, str, str],
            None,
        ],
        add_segment_to_session: Callable[[datetime.datetime, float, float], None],
    ):
        """Test _concatenate_fragments with a production failure case."""

        # Case 1 in log.txt

        #
        # 1. Add recording to the database
        #

        record_id = 1
        start_time = datetime.datetime.fromtimestamp(
            1750711989.34, tz=datetime.timezone.utc
        )
        adjusted_start_time = datetime.datetime.fromtimestamp(
            1750711979.34, tz=datetime.timezone.utc
        )
        end_time = datetime.datetime.fromtimestamp(
            1750711992.32, tz=datetime.timezone.utc
        )
        camera_identifier = "test1"
        thumbnail_path = "/tmp/thumbnail.jpg"
        add_recording_to_session(
            record_id,
            start_time,
            adjusted_start_time,
            end_time,
            camera_identifier,
            thumbnail_path,
        )

        #
        # 2. Concantenate the segments (fragments) for this recording
        #

        recording = Recording(
            id=record_id,
            start_time=start_time,
            start_timestamp=start_time.timestamp(),
            end_time=None,
            end_timestamp=None,
            date="2025-06-23",
            thumbnail=None,
            thumbnail_path=thumbnail_path,
            clip_path=None,
            objects=[],
        )

        # pylint: disable=protected-access
        with patch.object(recorder._storage, "get_session") as mock_get_session, patch(
            "shutil.move"
        ) as mock_file_move:
            # Return a list of fragments from the database session
            mock_get_session.return_value = get_db_session()

            # recorder runs concatenate_fragments in a thread
            concat_thread = RestartableThread(
                name="viseron.camera.test.concatenate_fragments",
                target=recorder._concatenate_fragments,
                args=(recording,),
                register=False,
            )
            concat_thread.start()

            #
            #  3. While concatenation thread is running insert a segment after a delay
            #

            segment_duration = 9.57
            segment_start = datetime.datetime.fromtimestamp(
                1750711982.00, tz=datetime.timezone.utc
            )
            # simulate completing in the middle of a recording
            # segment_start = start_time - datetime.timedelta(seconds=go_back)
            delay_before_insert = segment_duration - (
                start_time.timestamp() - segment_start.timestamp()
            )
            #
            #    SS    RS RE    SE
            #    |              |
            #    ------ SD ------
            #
            print(
                f"{start_time.timestamp()} \
                {segment_start.timestamp()} {segment_duration} {delay_before_insert}"
            )
            add_segment_to_session(segment_start, segment_duration, delay_before_insert)

            concat_thread.join()

            assert mock_file_move.call_count == 1

        assert recording.clip_path is not None
        (date, filename) = recording.clip_path.split("/")[-2:]
        assert date == recording.date
        assert filename == f"{recording.start_time.strftime('%H-%M-%S')}.mp4"
