# Repair Tools
CLI toolkit built to audit, repair, and report on digital preservation packages ("bags") for NYPL Preservica ingest workflows. 

## Features

- **Source-Target Synchronization**: Compares local source directories against target directories and/or Preservica contents to identify missing or unsuccessful ingests.
- **Automated Repairs**: Includes utilities to correct bag structures, generate missing manifiests, estbalishing symlinks to existing local assets for storage optimzation, and remove unnecessary files/directories from bags.
- **Workflow Management**: Supports deletion workflows and re-ingest checklists to track package status updates.

## Installation

This project uses [Poetry](https://python-poetry.org/) for dependency management.

```bash
# Clone the repository
git clone <repo_url>
cd prsv_repair_tools

# Install dependencies
poetry install

# Configure settings
# Copy the template to a usable INI file
cp compare_sources_template.ini compare_sources.ini
```
> [!NOTE]
> Ensure `compare_sources.ini` is configured with your specific `CACHE_PATH` before running audits.

## Usage

> [!WARNING]
> Under construction.
