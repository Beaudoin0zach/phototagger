-- List every album under a named Photos folder (searched at any depth),
-- one line per album: folder path TAB album name TAB comma-joined photo ids.
-- Ids (not names) are returned because album names are not unique across
-- folders ("Plants" exists both top-level and inside US / Colorado).
-- The folder name comparison trims whitespace: at least one real folder
-- ("Around the World ") carries a trailing space invisible in the UI.

on run argv
	set targetName to my trimmed(item 1 of argv)
	set collected to {}
	tell application "Photos"
		repeat with topFolder in folders
			set found to my findFolder(topFolder, targetName)
			if found is not missing value then
				my walkFolder(found, {}, collected)
				exit repeat
			end if
		end repeat
	end tell
	set AppleScript's text item delimiters to linefeed
	set joined to collected as text
	set AppleScript's text item delimiters to ""
	return joined
end run

on trimmed(value)
	set value to value as text
	repeat while value ends with " "
		if length of value is 1 then return ""
		set value to text 1 thru -2 of value
	end repeat
	repeat while value starts with " "
		if length of value is 1 then return ""
		set value to text 2 thru -1 of value
	end repeat
	return value
end trimmed

on findFolder(candidate, targetName)
	tell application "Photos"
		if my trimmed(name of candidate) is targetName then return candidate
		repeat with sub in folders of candidate
			set result to my findFolder(sub, targetName)
			if result is not missing value then return result
		end repeat
	end tell
	return missing value
end findFolder

on walkFolder(currentFolder, pathSegments, collected)
	tell application "Photos"
		repeat with sub in folders of currentFolder
			-- concatenation builds a fresh list, so recursion never aliases
			my walkFolder(sub, pathSegments & {my trimmed(name of sub)}, collected)
		end repeat
		repeat with alb in albums of currentFolder
			set idList to id of media items of alb
			set AppleScript's text item delimiters to ","
			set idsText to idList as text
			set AppleScript's text item delimiters to " / "
			set pathText to pathSegments as text
			set AppleScript's text item delimiters to ""
			set end of collected to pathText & tab & my trimmed(name of alb) & tab & idsText
		end repeat
	end tell
end walkFolder
