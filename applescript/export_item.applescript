on run argv
    if (count of argv) is not 3 then error "usage: export_item.applescript ALBUM ITEM_ID DESTINATION"
    set albumName to item 1 of argv
    set itemId to item 2 of argv
    set destinationPath to item 3 of argv

    tell application "Photos"
        set matchingAlbums to every album whose name is albumName
        if (count of matchingAlbums) is not 1 then error "Expected one Photos album named: " & albumName
        set targetAlbum to item 1 of matchingAlbums
        set matchingItems to every media item of targetAlbum whose id is itemId
        if (count of matchingItems) is not 1 then error "Expected one media item with id: " & itemId
        export matchingItems to (POSIX file destinationPath) with using originals
    end tell
end run

