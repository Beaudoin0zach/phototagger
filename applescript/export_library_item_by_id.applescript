-- Exports a media item addressed by id only (no positional index), so this
-- is immune to the library growing or shrinking between calls. Raises a
-- distinguishable "PHOTO_NOT_FOUND: <id>" error when the id no longer
-- resolves (e.g. the photo was deleted), so callers can tell that apart from
-- a transient automation failure.
on run argv
    if (count of argv) is not 2 then error "usage: export_library_item_by_id.applescript ITEM_ID DESTINATION"
    set itemId to item 1 of argv
    set destinationPath to item 2 of argv

    tell application "Photos"
        try
            set photoItem to media item id itemId
        on error
            error "PHOTO_NOT_FOUND: " & itemId
        end try
        export {photoItem} to (POSIX file destinationPath) with using originals
    end tell
end run
