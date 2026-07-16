on splitValues(inputText, delimiterText)
    if inputText is "" then return {}
    set previousDelimiters to AppleScript's text item delimiters
    set AppleScript's text item delimiters to delimiterText
    set outputValues to every text item of inputText
    set AppleScript's text item delimiters to previousDelimiters
    return outputValues
end splitValues

on run argv
    if (count of argv) is not 3 then error "usage: set_library_keywords.applescript ITEM_INDEX ITEM_ID KEYWORDS"
    set itemIndex to (item 1 of argv) as integer
    set itemId to item 2 of argv
    set keywordText to item 3 of argv
    set newKeywords to my splitValues(keywordText, character id 31)

    tell application "Photos"
        set photoItem to media item itemIndex
        if (id of photoItem) is not itemId then error "Photos library order changed at item: " & itemIndex
        set keywords of photoItem to newKeywords
    end tell
end run
