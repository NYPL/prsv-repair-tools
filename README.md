# Repair Tools

A CLI toolkit for auditing, repairing, and managing digital preservation packages for Preservica ingest workflows.

## Overview

This project provides a collection of scripts organized around common package management tasks: checking integrity, cleaning up bag structures, moving packages between locations, transcoding media, and generating checksums and manifests.

Scripts are designed to be run individually from the command line.

## Installation

Requires [Poetry](https://python-poetry.org/) for dependency management.

```bash
git clone <repo_url>
cd repair-tools
poetry install
```

## Features

### Package Auditing
- Check package sizes against a configurable threshold
- Identify and resolve duplicate packages in Preservica by comparing checksums and ingest dates

### Package Repair & Cleanup
- Correct malformed bag structures
- Delete empty folders, zero-byte files, and hidden system files
- Flatten nested media directories
- Remove transient folders from Preservica after ingest is confirmed

### Bag Construction
- Create standard bag directory structures
- Build symlink-based bags pointing to source assets (avoiding duplication)
- Generate and update `manifest-md5.txt` files
- Expand Excel-based package manifests into bag structures with sidecar JSON

### Media Processing
- Generate MD5 checksums for files within a package's data directories
- Transcode media files (WAV → FLAC, MOV → MKV) with lossless verification
- Download or transcode service copy MP4s from S3 or local sources

### Preservica Workflows
- Move packages between folders in Preservica by title
- Delete structural objects from Preservica

## Project Structure

```
repair_tools/
├── utils/           # Shared utilities (logging, CLI parsing, file ops, API helpers)
├── path_tools/      # Path and index searching helpers
└── *.py             # Individual CLI scripts
```

## Usage

Each script supports `--help` for available arguments:

```bash
poetry run <script-name> --help
```

> [!NOTE]
> Some scripts require a `credentials.ini` file with Preservica API credentials configured before use.
