# Workspace file storage

This document describes the workspace file storage functionality added to
Kragen. It covers the data model, object storage layout, HTTP API, Telegram
integration, web UI, access rules, migration, and operational behavior.

## Goals

The storage feature provides a single logical file tree shared by the web UI,
Telegram channel, API clients, and future integrations.

The main design goals are:

- Keep folder structure in PostgreSQL, where rename and move operations are
  cheap and transactional.
- Keep binary file contents in the configured S3-compatible object store.
- Use one service layer for web/API and Telegram writes, so both channels see
  the same folders and files.
- Preserve the existing `documents` table as a memory/RAG source rather than
  turning it into a general-purpose file manager.
- Make uploaded files available through stable logical metadata while object
  storage keys remain implementation details.

## High-level architecture

Kragen storage is split into two layers:

- **Logical file tree**: PostgreSQL table `storage_entries`.
- **Blob storage**: configured S3-compatible bucket, accessed through
  `src/kragen/storage/object_store.py`.

Folders are database rows only. They are not represented by placeholder objects
in S3/MinIO. Empty folders therefore appear in the Kragen API and web UI, but
not in MinIO object listings.

Files have both:

- a `storage_entries` row with name, parent, path cache, source, size, MIME type,
  and content hash;
- an S3 object containing the raw bytes.

```text
Client / channel
      |
      v
FastAPI routes or Telegram adapter
      |
      v
file_storage service
      |
      +--> PostgreSQL: storage_entries
      |
      +--> S3-compatible object store: file bytes
      |
      +--> PostgreSQL: documents, when RAG indexing metadata is requested
```

## Database model

The logical file tree is stored in `storage_entries`, introduced by
`alembic/versions/004_storage_entries.py` and mapped by
`src/kragen/models/storage.py`.

### `storage_entries`

Each row is either a folder or a file.

| Column | Purpose |
| --- | --- |
| `id` | Stable entry identifier. File object keys include this value. |
| `workspace_id` | Workspace/tenant boundary. All API access is scoped by this field. |
| `parent_id` | Parent folder entry, or `NULL` for root-level entries. |
| `kind` | Either `file` or `folder`. |
| `name` | Single path segment shown to users. Names cannot contain `/` or NUL. |
| `path_cache` | Cached absolute logical path, such as `/public/report.pdf`. |
| `object_key` | S3 object key for file entries. `NULL` for folders. |
| `uri` | S3 URI returned by the object store helper. `NULL` for folders. |
| `size_bytes` | File size in bytes. `NULL` for folders. |
| `mime_type` | File content type when known. |
| `content_hash` | SHA-256 digest of file bytes. |
| `source_type` | Origin marker, for example `upload`, `telegram`, or `web`. |
| `created_by_user_id` | User that created the entry, if known. |
| `metadata` | Channel-specific or client-specific JSON metadata. |
| `created_at` / `updated_at` | Entry timestamps. |
| `deleted_at` | Soft-delete marker. Active entries have `NULL`. |

Important indexes and constraints:

- `ix_storage_entries_workspace_parent` supports folder listing.
- `ix_storage_entries_workspace_path` supports path-oriented lookups and
  descendant path updates.
- `ix_storage_entries_content_hash` supports content-hash inspection.
- `uq_storage_entries_child_name_active` prevents duplicate active sibling names
  under a non-root parent.
- `uq_storage_entries_root_name_active` prevents duplicate active root-level
  names per workspace.
- `ck_storage_entries_kind` restricts `kind` to `file` or `folder`.

### Relationship to `documents`

`documents` remains the model for memory/RAG indexing. It is not the canonical
file manager table.

When `create_document=true` is used during upload, Kragen creates a `Document`
row alongside the `StorageEntry` row:

- `Document.source_ref` points at the S3 URI.
- `Document.content_hash` stores the same SHA-256 digest.
- `Document.metadata.storage_entry_id` stores the logical file entry ID.

This is intentionally a soft relationship. The logical file tree can exist
without RAG indexing, and RAG documents can continue to support sources outside
the file manager.

## Object storage layout

File bytes are stored under stable technical keys:

```text
workspaces/{workspace_id}/files/{entry_id}/{content_hash}
```

The key intentionally does not include the user-visible folder path or file
name. This means:

- renaming a file does not copy the object;
- moving a file to another folder does not copy the object;
- user-visible Unicode names do not need to be encoded into S3 keys;
- duplicate file names are constrained by the logical tree, not by S3 paths.

Folders do not create S3 keys. MinIO will show file objects once a file is
uploaded, but it will not show empty logical folders.

## Service layer

Shared storage behavior lives in `src/kragen/services/file_storage.py`.

This module is the source of truth for logical tree operations and is used by
both FastAPI routes and the Telegram adapter.

Main functions:

- `validate_entry_name(name)`: validates one path segment.
- `get_entry(db, workspace_id, entry_id)`: loads an active entry.
- `list_entries(db, workspace_id, parent_id)`: lists direct active children.
- `create_folder(...)`: creates a folder under a parent.
- `ensure_folder_path(...)`: creates missing folders for an absolute path and
  returns the deepest folder.
- `create_file_from_bytes(...)`: writes bytes to object storage and creates a
  file entry, optionally creating a `Document`.
- `update_entry(...)`: renames and/or moves an entry.
- `soft_delete_entry(...)`: marks an entry and folder descendants as deleted.

Errors are represented by storage-specific exceptions:

- `InvalidStorageName`
- `StorageEntryNotFound`
- `StorageEntryConflict`
- `InvalidStorageMove`

The HTTP layer maps these to appropriate response codes.

## HTTP API

The storage API is implemented in `src/kragen/api/routes/files.py`.

All storage endpoints require a valid user and call `ensure_workspace_access`
before returning or modifying workspace-scoped data.

### List folder entries

```http
GET /files?workspace_id={workspace_id}&parent_id={parent_id}
```

Parameters:

- `workspace_id` is required.
- `parent_id` is optional. If omitted, the root folder is listed.

Returns a list of `StorageEntryOut` rows.

Example:

```bash
curl -H "Authorization: Bearer $KRAGEN_TOKEN" \
  "http://127.0.0.1:8000/files?workspace_id=$WORKSPACE_ID"
```

### Create a folder

```http
POST /files/folders
Content-Type: application/json
```

Body:

```json
{
  "workspace_id": "00000000-0000-0000-0000-000000001111",
  "parent_id": null,
  "name": "public"
}
```

Creates a logical folder. It does not create a MinIO object.

Audit event:

- `storage.folder_created`

### Upload a file

```http
POST /files/upload
Content-Type: multipart/form-data
```

Form fields:

- `workspace_id`: required workspace UUID.
- `parent_id`: optional folder UUID.
- `create_document`: optional boolean, default `true`.
- `file`: uploaded file body.

Behavior:

1. Validates workspace access.
2. Reads the upload bytes.
3. Computes SHA-256.
4. Writes the blob to S3-compatible object storage.
5. Creates a `storage_entries` file row.
6. Creates a `documents` row when `create_document=true`.

Audit event:

- `storage.file_uploaded`

### Get entry metadata

```http
GET /files/{entry_id}?workspace_id={workspace_id}
```

Returns a single `StorageEntryOut` row.

### Rename or move an entry

```http
PATCH /files/{entry_id}?workspace_id={workspace_id}
Content-Type: application/json
```

Body fields:

- `name`: optional new name.
- `parent_id`: optional new parent folder ID. `null` moves the entry to root
  when the field is explicitly present.

Behavior:

- Checks duplicate sibling names.
- Prevents moving a folder under itself or one of its descendants.
- Updates `path_cache` for descendants when a folder path changes.
- Does not copy or rename the S3 object.

Audit event:

- `storage.entry_updated`

### Soft-delete an entry

```http
DELETE /files/{entry_id}?workspace_id={workspace_id}
```

Behavior:

- Sets `deleted_at` and `updated_at`.
- If the entry is a folder, active descendants are soft-deleted as well.
- Does not delete S3 objects yet.

Audit event:

- `storage.entry_deleted`

### Download a file

```http
GET /files/{entry_id}/download?workspace_id={workspace_id}
```

Behavior:

- Validates workspace access.
- Loads file bytes from object storage by `object_key`.
- Returns a response with `Content-Disposition: attachment`.

Folders are not downloadable and return `400`.

## Web UI

The static web UI in `web/index.html` includes a `Files` tab for the logical
file tree.

The updated storage UI provides:

- a table view with columns for name, kind, size, source, and update time;
- breadcrumb navigation for the current folder;
- current-folder filtering;
- a details panel for the selected entry;
- folder creation;
- file upload;
- open folder;
- download file;
- rename;
- soft delete;
- attach selected path to chat;
- copy selected path.

The UI uses the same HTTP API described above. It does not talk directly to S3.

### Workspace selection

The file manager uses the currently selected workspace from the shared
workspace dropdown. Storage entries created by Telegram appear in the web UI
only when the web user has access to the Telegram workspace.

If a folder exists in Telegram but not in the web UI, check:

1. whether the web token can list the Telegram workspace through
   `GET /workspaces`;
2. whether the UI workspace dropdown is set to the same workspace used by
   Telegram;
3. whether `GET /files?workspace_id=...` returns the expected rows.

## Telegram integration

The Telegram adapter in `src/kragen/channels/telegram_adapter.py` uses the same
storage service layer as the HTTP API.

### Commands

The adapter registers the bot command menu through `setMyCommands`.

Supported commands:

- `/start`: connect the chat to Kragen.
- `/new`: start a new session.
- `/whoami`: show the chat, session, workspace, and user binding.
- `/sessions`: list recent sessions.
- `/tasks`: list recent tasks for the current session.
- `/files`: list root files and folders.
- `/ls`: alias for `/files`.
- `/mkdir <name>`: create a root folder.
- `/help`: show command help.
- `/commands`: show command help.

### Folder creation

`/mkdir <name>` creates a root-level logical folder in the configured Telegram
workspace. The source type is `telegram`, and the entry metadata includes the
Telegram chat ID.

Example:

```text
/mkdir public
```

Creates:

```text
/public
```

### Listing root entries

`/files` and `/ls` list active root-level entries in the Telegram workspace.

### Document upload

When a user sends a Telegram document, the adapter:

1. Resolves or creates the chat binding.
2. Downloads the document from Telegram using `getFile`.
3. Extracts a target logical folder path from the caption, if present.
4. Creates the target folder path when it is missing.
5. Uploads the file bytes through `file_storage.create_file_from_bytes`.
6. Creates a `Document` row for RAG metadata.
7. Persists a direct Telegram exchange so the web UI can show the event in the
   same session history.
8. Sends a confirmation with file name, size, path, and entry ID.

Default target path:

```text
/Inbox/Telegram
```

Caption-based target path:

```text
put this file in /public
```

Stores the file under:

```text
/public/{filename}
```

The path extractor supports absolute paths beginning with `/`, including
Unicode path segments. It strips common trailing punctuation from the extracted
path.

If a file with the same name already exists in the target folder, the adapter
retries with a suffix based on the Telegram `file_unique_id`.

### Telegram metadata

Telegram-created file entries include metadata such as:

- `telegram_chat_id`
- `telegram_message_id`
- `telegram_update_id`
- `telegram_username`
- `telegram_document_file_id`
- `telegram_document_file_unique_id`
- `telegram_document_file_name`
- `telegram_storage_target_path`

The persisted chat message metadata includes:

- `storage_entry_id`
- `document_id`
- `telegram_document_uri`

## Session visibility across channels

The storage work also changed session visibility so the web UI can show
Telegram conversations when the current web user has workspace access.

Updated behavior:

- `GET /sessions?workspace_id=...` returns sessions for that workspace after
  `ensure_workspace_access` succeeds.
- `GET /sessions/{session_id}` checks workspace access instead of requiring the
  session owner to match the current user.
- `GET /sessions/{session_id}/messages` checks workspace access.
- `POST /sessions/{session_id}/messages` checks workspace access before posting.

This allows a workspace member to inspect or continue sessions that originated
from Telegram, while still enforcing workspace-level RBAC.

## Access control

Storage authorization is workspace-based.

The key rule is:

```text
If a user can access a workspace, the user can access storage entries and
sessions scoped to that workspace.
```

Access is enforced through `ensure_workspace_access`, which allows:

- admins;
- workspace owners;
- users listed in `workspace_members`.

Operationally, if a web user cannot see Telegram-created folders or sessions,
ensure that the web user is a member of the Telegram workspace.

## Migrations and deployment

The storage feature requires the Alembic migration:

```text
004_storage_entries
```

Apply migrations before starting code that uses storage entries:

```bash
KRAGEN_CONFIG_FILE=/home/srg/projects/kragen/configs/kragen.yaml \
  /home/srg/projects/kragen/.venv/bin/alembic upgrade head
```

In systemd deployments, the service environment may provide the live database
URL or password. Use the same environment as the service when running
migrations:

```bash
set -a
. /etc/kragen/kragen-service.env
set +a

KRAGEN_CONFIG_FILE=/home/srg/projects/kragen/configs/kragen.yaml \
  /home/srg/projects/kragen/.venv/bin/alembic upgrade head
```

Then restart the combined service:

```bash
sudo systemctl restart kragen-service
```

If the migration is not applied, storage commands fail with PostgreSQL errors
similar to:

```text
UndefinedTableError: relation "storage_entries" does not exist
```

## MinIO and S3 behavior

MinIO shows object-store keys, not Kragen's logical folder tree.

Expected behavior:

- Empty folders created in Telegram or the web UI are visible in Kragen only.
- Empty folders are not visible in MinIO.
- Uploaded files are visible in MinIO under technical keys such as
  `workspaces/{workspace_id}/files/{entry_id}/{content_hash}`.
- Moving or renaming a file changes only the database row. MinIO object keys do
  not change.

This is intentional and avoids expensive object copies for user-visible file
tree operations.

## Auditing

The HTTP storage API writes audit events for user-facing operations:

- `storage.folder_created`
- `storage.file_uploaded`
- `storage.entry_updated`
- `storage.entry_deleted`

Audit payloads include the entry ID and logical path. File upload events also
include the S3 URI and optional document ID.

## Tests

The storage changes are covered by focused tests:

- `tests/test_files_rbac.py`: verifies workspace access checks and storage
  route behavior.
- `tests/test_telegram_adapter.py`: verifies command help, bot command payloads,
  file-name sanitization, and Telegram caption target-path extraction.
- `tests/test_sessions_workspace_access.py`: verifies workspace-scoped session
  and message access across channels.
- `tests/test_telegram_bindings.py`: verifies Telegram update staleness logic.

## Troubleshooting

### Telegram folder exists but web UI is empty

Check that the web user can access the Telegram workspace:

```bash
curl -H "Authorization: Bearer $WEB_TOKEN" \
  "http://127.0.0.1:8000/workspaces"
```

Then list files in the Telegram workspace:

```bash
curl -H "Authorization: Bearer $WEB_TOKEN" \
  "http://127.0.0.1:8000/files?workspace_id=$TELEGRAM_WORKSPACE_ID"
```

If the first command does not show the workspace, add the web user to
`workspace_members`.

### `/ls` or `/mkdir` fails with `storage_entries does not exist`

Run Alembic migrations with the same environment as the service, then restart
`kragen-service`.

### File was uploaded to `/Inbox/Telegram` instead of the requested folder

The target path is extracted from the Telegram document caption. The caption
must contain an absolute path such as `/public` or `/public/reports`.

Example:

```text
put this file in /public
```

If no absolute path is present, Kragen uses `/Inbox/Telegram`.

### Folder appears in Kragen but not in MinIO

This is expected for empty folders. Folders are database rows, not S3 objects.

### Rename or move does not change the MinIO key

This is expected. Logical path changes update `storage_entries.path_cache`.
Object keys are stable and independent from user-visible paths.

## Current limitations

- The web UI works as a static page and does not yet provide drag-and-drop move
  operations.
- Delete is soft-delete only. Object garbage collection is not implemented.
- Downloads are streamed through the API; signed URLs are not implemented yet.
- File versioning is not implemented. The current model stores one active file
  object per file entry.
- Folder listing is direct-child only. Recursive tree browsing is implemented
  client-side through navigation, not through a recursive API.
- Telegram storage commands currently cover root listing and root folder
  creation. More advanced commands such as `/cd`, `/move`, `/rename`, and
  `/delete` can be added on top of the same service layer.

