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

-- Atomically removes any of REMOVE_KEYWORDS that are present on the media
-- item identified by ITEM_ID (case-insensitive compare), leaving everything
-- else untouched -- including keywords added by hand since PhotoTagger last
-- wrote to this photo. Addressed by id only. Returns the before/after lists.
on run argv
    if (count of argv) is not 2 then error "usage: remove_library_keywords_by_id.applescript ITEM_ID REMOVE_KEYWORDS"
    set itemId to item 1 of argv
    set removeKeywordsText to item 2 of argv
    set removeKeywords to my splitValues(removeKeywordsText, character id 31)

    tell application "Photos"
        set photoItem to media item id itemId
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
