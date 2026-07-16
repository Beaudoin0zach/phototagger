tell application "Photos"
    set albumNames to name of every album
end tell

set AppleScript's text item delimiters to linefeed
set outputText to albumNames as text
set AppleScript's text item delimiters to ""
return outputText

