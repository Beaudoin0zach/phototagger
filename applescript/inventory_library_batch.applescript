on joinValues(theValues, delimiterText)
    set previousDelimiters to AppleScript's text item delimiters
    set AppleScript's text item delimiters to delimiterText
    set joinedText to theValues as text
    set AppleScript's text item delimiters to previousDelimiters
    return joinedText
end joinValues

on run argv
    if (count of argv) is less than 2 or (count of argv) is greater than 3 then error "usage: inventory_library_batch.applescript START COUNT [ORDER]"
    set startIndex to (item 1 of argv) as integer
    set batchCount to (item 2 of argv) as integer
    set traversalOrder to "ascending"
    if (count of argv) is 3 then set traversalOrder to item 3 of argv
    if startIndex is less than 1 then error "START must be at least 1"
    if batchCount is less than 1 then error "COUNT must be at least 1"
    if traversalOrder is not "ascending" then
        if traversalOrder is not "descending" then error "ORDER must be ascending or descending"
    end if
    set fieldSeparator to character id 30
    set keywordSeparator to character id 31
    set rowSeparator to character id 29

    tell application "Photos"
        set totalCount to count of media items
        if startIndex is greater than totalCount then return ""
        set itemIndexes to {}
        if traversalOrder is "descending" then
            set endIndex to startIndex - batchCount + 1
            if endIndex is less than 1 then set endIndex to 1
            repeat with itemIndex from startIndex to endIndex by -1
                set end of itemIndexes to itemIndex as integer
            end repeat
        else
            set endIndex to startIndex + batchCount - 1
            if endIndex is greater than totalCount then set endIndex to totalCount
            repeat with itemIndex from startIndex to endIndex
                set end of itemIndexes to itemIndex as integer
            end repeat
        end if
        set outputRows to {}
        repeat with itemIndexValue in itemIndexes
            set itemIndex to itemIndexValue as integer
            set photoItem to media item itemIndex
            set itemId to id of photoItem
            set itemFilename to filename of photoItem
            set itemDate to date of photoItem
            set itemKeywords to keywords of photoItem
            if itemKeywords is missing value then set itemKeywords to {}
            set keywordText to my joinValues(itemKeywords, keywordSeparator)
            set end of outputRows to itemId & fieldSeparator & itemFilename & fieldSeparator & keywordText & fieldSeparator & (itemIndex as text) & fieldSeparator & (itemDate as text)
        end repeat
    end tell

    return my joinValues(outputRows, rowSeparator)
end run
