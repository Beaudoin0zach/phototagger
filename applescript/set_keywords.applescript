on splitValues(inputText, delimiterText)
    if inputText is "" then return {}
    set previousDelimiters to AppleScript's text item delimiters
    set AppleScript's text item delimiters to delimiterText
    set outputValues to every text item of inputText
    set AppleScript's text item delimiters to previousDelimiters
    return outputValues
end splitValues

on run argv
    if (count of argv) is not 3 then error "usage: set_keywords.applescript ALBUM ITEM_ID KEYWORDS"
    set albumName to item 1 of argv
    set itemId to item 2 of argv
    set keywordText to item 3 of argv
    set newKeywords to my splitValues(keywordText, character id 31)

    tell application "Photos"
        set matchingAlbums to every album whose name is albumName
        if (count of matchingAlbums) is not 1 then error "Expected one Photos album named: " & albumName
        set targetAlbum to item 1 of matchingAlbums
        set matchingItems to every media item of targetAlbum whose id is itemId
        if (count of matchingItems) is not 1 then error "Expected one media item with id: " & itemId
        set keywords of item 1 of matchingItems to newKeywords
    end tell
end run

