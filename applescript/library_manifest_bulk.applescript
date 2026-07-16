on joinValues(theValues, delimiterText)
    set previousDelimiters to AppleScript's text item delimiters
    set AppleScript's text item delimiters to delimiterText
    set joinedText to theValues as text
    set AppleScript's text item delimiters to previousDelimiters
    return joinedText
end joinValues

-- Fetches the id, filename, and capture date of EVERY media item as three
-- bulk Apple events (Photos evaluates each list internally), instead of one
-- positional lookup per item. Positional per-item reads cost O(index) each,
-- which made a full-library sweep O(n^2) -- hours; this returns in seconds.
-- The three lists are fetched back-to-back; ids are authoritative, and a
-- concurrent library change can at worst misalign a cosmetic filename/date.
on run
    set fieldSeparator to character id 30
    set rowSeparator to character id 29

    tell application "Photos"
        set allIds to id of every media item
        set allFilenames to filename of every media item
        set allDates to date of every media item
    end tell

    set idCount to count of allIds
    set nameCount to count of allFilenames
    set dateCount to count of allDates
    set outputRows to {}
    repeat with itemIndex from 1 to idCount
        set itemId to item itemIndex of allIds
        set itemFilename to ""
        if itemIndex is less than or equal to nameCount then
            set itemFilename to (item itemIndex of allFilenames) as text
        end if
        set itemDate to ""
        if itemIndex is less than or equal to dateCount then
            try
                set itemDate to (item itemIndex of allDates) as text
            end try
        end if
        set end of outputRows to itemId & fieldSeparator & itemFilename & fieldSeparator & itemDate
    end repeat

    return my joinValues(outputRows, rowSeparator)
end run
