on joinValues(theValues, delimiterText)
    set previousDelimiters to AppleScript's text item delimiters
    set AppleScript's text item delimiters to delimiterText
    set joinedText to theValues as text
    set AppleScript's text item delimiters to previousDelimiters
    return joinedText
end joinValues

on run argv
    if (count of argv) is not 1 then error "usage: inventory_album.applescript ALBUM"
    set albumName to item 1 of argv
    set fieldSeparator to character id 30
    set keywordSeparator to character id 31
    set rowSeparator to character id 29

    tell application "Photos"
        set matchingAlbums to every album whose name is albumName
        if (count of matchingAlbums) is 0 then error "Photos album not found: " & albumName
        if (count of matchingAlbums) is greater than 1 then error "More than one Photos album is named: " & albumName
        set targetAlbum to item 1 of matchingAlbums
        set albumItems to every media item of targetAlbum
        set outputRows to {}
        repeat with photoItem in albumItems
            set itemId to id of photoItem
            set itemFilename to filename of photoItem
            set itemKeywords to keywords of photoItem
            if itemKeywords is missing value then set itemKeywords to {}
            set keywordText to my joinValues(itemKeywords, keywordSeparator)
            set end of outputRows to itemId & fieldSeparator & itemFilename & fieldSeparator & keywordText
        end repeat
    end tell

    return my joinValues(outputRows, rowSeparator)
end run
