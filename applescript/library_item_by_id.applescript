on joinValues(theValues, delimiterText)
    set previousDelimiters to AppleScript's text item delimiters
    set AppleScript's text item delimiters to delimiterText
    set joinedText to theValues as text
    set AppleScript's text item delimiters to previousDelimiters
    return joinedText
end joinValues

-- Reads a media item by id only -- no positional index, so this is immune to
-- the library growing or shrinking between calls. Returns "NOT_FOUND" rather
-- than raising when the id no longer resolves (e.g. the photo was deleted),
-- so callers can distinguish a genuinely-gone photo from an automation error.
on run argv
    if (count of argv) is not 1 then error "usage: library_item_by_id.applescript ITEM_ID"
    set itemId to item 1 of argv
    set fieldSeparator to character id 30
    set keywordSeparator to character id 31

    tell application "Photos"
        try
            set photoItem to media item id itemId
        on error
            return "NOT_FOUND"
        end try
        set itemFilename to filename of photoItem
        set itemDate to date of photoItem
        set itemKeywords to keywords of photoItem
        if itemKeywords is missing value then set itemKeywords to {}
        set keywordText to my joinValues(itemKeywords, keywordSeparator)
        return "FOUND" & fieldSeparator & itemFilename & fieldSeparator & keywordText & fieldSeparator & (itemDate as text)
    end tell
end run
