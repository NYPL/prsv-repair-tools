import pytest
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import repair_tools.download_sc_mp4 as download_sc_mp4

@pytest.fixture
def mock_fs(tmp_path):
    pkg_dir = tmp_path / "123456"
    pm_dir = pkg_dir / "data" / "PreservationMasters"
    sc_dir = pkg_dir / "data" / "ServiceCopies"
    
    pm_dir.mkdir(parents=True)
    sc_dir.mkdir(parents=True)
    
    return {
        "root": tmp_path,
        "pkg": pkg_dir,
        "pm": pm_dir,
        "sc": sc_dir
    }

@pytest.fixture
def mock_s3_client():
    """Mocks the boto3 S3 client to prevent actual network calls."""
    with patch("repair_tools.download_sc.boto3.client") as mock:
        yield mock

# find_pm_files tests

def test_find_pm_files_identifies_media(mock_fs):
    """Should find valid video/audio files and ignore others."""
    # valid files
    (mock_fs["pm"] / "video.mkv").touch()
    (mock_fs["pm"] / "audio.flac").touch()
    (mock_fs["pm"] / "movie.mov").touch()
    
    # invalid files (to be ignored)
    (mock_fs["pm"] / "metadata.xml").touch()
    (mock_fs["pm"] / "thumbs.db").touch()

    found_files = download_sc_mp4.find_pm_files(mock_fs["pkg"])
    found_names = sorted([f.name for f in found_files])

    assert found_names == ["audio.flac", "movie.mov", "video.mkv"]

def test_find_pm_files_recursive(mock_fs):
    """Should find files inside subdirs."""
    sub_dir = mock_fs["pm"] / "subdir"
    sub_dir.mkdir()
    (sub_dir / "nested_video.dv").touch()

    found_files = download_sc_mp4.find_pm_files(mock_fs["pkg"])
    assert len(found_files) == 1
    assert found_files[0].name == "nested_video.dv"

def test_find_pm_files_missing_dir(tmp_path):
    """Should return empty list if dir doesn't exist."""
    empty_pkg = tmp_path / "999999"
    files = download_sc_mp4.find_pm_files(empty_pkg)
    assert files == []

# build_s3_index

def test_build_s3_index_from_bucket(mock_s3_client, tmp_path):
    """Should fetch from S3 if no local cache exists."""
    index_path = tmp_path / "s3_index.json"
    bucket_name = "my-test-bucket"

    # Mock S3 response with pagination
    mock_paginator = MagicMock()
    mock_s3_client.return_value.get_paginator.return_value = mock_paginator
    mock_paginator.paginate.return_value = [
        {"Contents": [{"Key": "folder/video1_sc.mp4"}, {"Key": "folder/audio1_sc.mp4"}]}
    ]

    result = download_sc_mp4.build_s3_index(bucket_name, index_path)

    mock_s3_client.return_value.get_paginator.assert_called_once_with("list_objects_v2")
    
    # result dictionary is correct (Filename -> Full Key)
    expected = {
        "video1_sc.mp4": "folder/video1_sc.mp4",
        "audio1_sc.mp4": "folder/audio1_sc.mp4"
    }
    assert result == expected

    # cache file was written
    assert index_path.exists()
    with open(index_path) as f:
        data = json.load(f)
        assert len(data) == 2

def test_build_s3_index_from_cache(mock_s3_client, tmp_path):
    """Should load from local JSON if it exists, skipping S3."""
    index_path = tmp_path / "s3_index.json"
    
    cache_data = [{"Key": "cached_video.mp4"}]
    with open(index_path, "w") as f:
        json.dump(cache_data, f)

    result = download_sc_mp4.build_s3_index("bucket", index_path)

    # S3 was NOT called
    mock_s3_client.return_value.get_paginator.assert_not_called()

    assert "cached_video.mp4" in result

# download_file_worker

def test_download_worker_success(mock_fs, mock_s3_client):
    """Should download file if it doesn't exist."""
    dest = mock_fs["sc"] / "new_video.mp4"
    task = ("bucket", "key/video.mp4", dest, False) # False = Not Test Mode

    msg, success = download_sc_mp4.download_file_worker(task)

    assert success is True
    assert "Downloaded" in msg
    # S3 download call
    mock_s3_client.return_value.download_file.assert_called_once_with(
        "bucket", "key/video.mp4", str(dest)
    )

def test_download_worker_skips_existing(mock_fs, mock_s3_client):
    """Should skip download if file exists."""
    dest = mock_fs["sc"] / "existing.mp4"
    dest.touch() 
    
    task = ("bucket", "key", dest, False)
    msg, success = download_sc_mp4.download_file_worker(task)

    assert success is True
    assert "Skipped" in msg
    mock_s3_client.return_value.download_file.assert_not_called()

def test_download_worker_test_mode(mock_fs, mock_s3_client):
    """Should just log message in test mode."""
    dest = mock_fs["sc"] / "test.mp4"
    task = ("bucket", "key", dest, True) # True = Test Mode

    msg, success = download_sc_mp4.download_file_worker(task)

    assert success is True
    assert "[TEST]" in msg
    mock_s3_client.return_value.download_file.assert_not_called()

# create_sc_worker

@patch("repair_tools.download_sc.vp.convert_to_mp4")
def test_create_sc_worker(mock_convert, mock_fs):
    """Should find PM files and call video_processing module."""
    pm_file = mock_fs["pm"] / "master.mkv"
    pm_file.touch()

    pkg_path, success, err = download_sc_mp4.create_sc_worker(mock_fs["pkg"])

    assert success is True
    assert pkg_path == mock_fs["pkg"]
    
    mock_convert.assert_called_once()
    args, _ = mock_convert.call_args
    assert args[0] == pm_file.resolve()          # Input path
    assert args[1] == "master.mkv"               # Filename
    assert args[2] == mock_fs["sc"]              # Output dir