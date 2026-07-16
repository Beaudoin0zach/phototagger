on run argv
    if (count of argv) is not 0 then error "usage: library_count.applescript"
    tell application "Photos" to return count of media items
end run
