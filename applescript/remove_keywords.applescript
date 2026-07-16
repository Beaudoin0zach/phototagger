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

-- Album-scoped counterpart to remove_library_keywords_by_id.applescript: used
-- by rollback to surgically remove exactly the tags PhotoTagger added,
-- leaving anything else (including concurrent manual additions) untouched.
on run argv
    if (count of argv) is not 3 then error "usage: remove_keywords.applescript ALBUM ITEM_ID REMOVE_KEYWORDS"
    set albumName to item 1 of argv
    set itemId to item 2 of argv
    set removeKeywordsText to item 3 of argv
    set removeKeywords to my splitValues(removeKeywordsText, character id 31)

    tell application "Photos"
        set matchingAlbums to every album whose name is albumName
        if (count of matchingAlbums) is not 1 then error "Expected one Photos album named: " & albumName
        set targetAlbum to item 1 of matchingAlbums
        set matchingItems to every media item of targetAlbum whose id is itemId
        if (count of matchingItems) is not 1 then error "Expected one media item with id: " & itemId
        set photoItem to item 1 of matchingItems
        set currentKeywords to keywords of photoItem
        if currentKeywords is missing value then set currentKeywords to {}
        set keptKeywords to {}
        set removedAny to false
        repeat with existingKeyword in currentKeywords
            set existingText to existingKeyword as text
            set shouldRemove to false
            repeat with tagToRemove in removeKeywords
                if (my lowercaseText(existingText)) is (my lowercaseText(tagToRemove as text)) then
                    set shouldRemove to true
                    set removedAny to true
                    exit repeat
                end if
            end repeat
            if not shouldRemove then set end of keptKeywords to existingText
        end repeat
        if removedAny then set keywords of photoItem to keptKeywords
    end tell

    set fieldSeparator to character id 30
    set keywordSeparator to character id 31
    return (my joinValues(currentKeywords, keywordSeparator)) & fieldSeparator & (my joinValues(keptKeywords, keywordSeparator))
end run
