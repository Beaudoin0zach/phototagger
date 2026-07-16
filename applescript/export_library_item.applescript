on run argv
    if (count of argv) is not 3 then error "usage: export_library_item.applescript ITEM_INDEX ITEM_ID DESTINATION"
    set itemIndex to (item 1 of argv) as integer
    set itemId to item 2 of argv
    set destinationPath to item 3 of argv

    tell application "Photos"
        set photoItem to media item itemIndex
        if (id of photoItem) is not itemId then error "Photos library order changed at item: " & itemIndex
        export {photoItem} to (POSIX file destinationPath) with using originals
    end tell
end run
