use framework "Foundation"

on lowercaseText(theText)
    return ((current application's NSString's stringWithString:theText)'s lowercaseString()) as text
end lowercaseText

on splitValues(inputText, delimiterText)
    if inputText is "" then return {}
    set previousDelimiters to AppleScript's text item delimiters
    set AppleScript's text item delimiters to delimiterText
    set outputValues to every text item of inputText
    set AppleScript's text item delimiters to previousDelimiters
    return outputValues
end splitValues

on joinValues(theValues, delimiterText)
    set previousDelimiters to AppleScript's text item delimiters
    set AppleScript's text item delimiters to delimiterText
    set joinedText to theValues as text
    set AppleScript's text item delimiters to previousDelimiters
    return joinedText
end joinValues

-- Album-scoped counterpart to sync_library_keywords_by_id.applescript: reads
-- the current keyword list fresh and adds anything from NEW_KEYWORDS that is
-- missing (case-insensitive), leaving concurrent additions untouched.
on run argv
    if (count of argv) is not 3 then error "usage: sync_keywords.applescript ALBUM ITEM_ID NEW_KEYWORDS"
    set albumName to item 1 of argv
    set itemId to item 2 of argv
    set newKeywordsText to item 3 of argv
    set newKeywords to my splitValues(newKeywordsText, character id 31)

    tell application "Photos"
        set matchingAlbums to every album whose name is albumName
        if (count of matchingAlbums) is not 1 then error "Expected one Photos album named: " & albumName
        set targetAlbum to item 1 of matchingAlbums
        set matchingItems to every media item of targetAlbum whose id is itemId
        if (count of matchingItems) is not 1 then error "Expected one media item with id: " & itemId
        set photoItem to item 1 of matchingItems
        set currentKeywords to keywords of photoItem
        if currentKeywords is missing value then set currentKeywords to {}
        -- copy, never alias: AppleScript lists are reference-assigned, and
        -- appending through an alias would corrupt the returned before-list
        copy currentKeywords to mergedKeywords
        set addedAny to false
        repeat with newKeyword in newKeywords
            set newKeywordText to newKeyword as text
            set isPresent to false
            repeat with existingKeyword in currentKeywords
                if (my lowercaseText(existingKeyword as text)) is (my lowercaseText(newKeywordText)) then
                    set isPresent to true
                    exit repeat
                end if
            end repeat
            if not isPresent then
                set end of mergedKeywords to newKeywordText
                set addedAny to true
            end if
        end repeat
        if addedAny then set keywords of photoItem to mergedKeywords
    end tell

    set fieldSeparator to character id 30
    set keywordSeparator to character id 31
    return (my joinValues(currentKeywords, keywordSeparator)) & fieldSeparator & (my joinValues(mergedKeywords, keywordSeparator))
end run
