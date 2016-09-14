# Telecat
A telegram bot to control and get status from hashcat.

## Setup/Usage
Just add your user id and bot token in config.json. 

Bot supports:
* /launch
* /stats (with /stats interval and /stats STOP for automatic status messages)
* /pause and /resume
* /quit
* Downloading sent text files.

## Example:
```
Gerry, [Sep 14, 2016, 1:13 PM]: 
/launch -w 3 -m 0 /home/gerry/test /home/gerry/wordlists/merged.txt -r /home/gerry/rules/hob064.rule

hashcat controller, [Sep 14, 2016, 1:13 PM]: 
Launched a new scan: /usr/local/bin/hashcat --quiet --status --machine-readable -w 3 -m 0 /home/gerry/test /home/gerry/wordlists/merged.txt -r /home/gerry/rules/hob064.rule -o /tmp/tmpT8BCiN

hashcat controller, [Sep 14, 2016, 1:13 PM]: 
Session Complete
Current Status: Cracked
Current Speeds:
 Device: 0 Count: 5767168 ms: 26.227914
 Device: 1 Count: 5767168 ms: 26.125062
 Device: 2 Count: 5767168 ms: 26.145359
 Device: 3 Count: 5767168 ms: 26.268141
Current Keyspace Unit: 46410258
Progress: 2983130496/5504663168
Recovered Hashes: 1/1
Recovered Salts: 1/1
Tempratures:
 Device: 0 Temp: 31
 Device: 1 Temp: 30
 Device: 2 Temp: 32
 Device: 3 Temp: 29
Runtimes:
 Device: 0 ms: 72.047792
 Device: 1 ms: 72.511568
 Device: 2 ms: 75.192640
 Device: 3 ms: 74.723120
Command Line:
 /usr/local/bin/hashcat --quiet --status --machine-readable -w 3 -m 0 /home/gerry/test /home/gerry/wordlists/merged.txt -r /home/gerry/rules/hob064.rule -o /tmp/tmpT8BCiN
Output
b7e283a09511d95d6eac86e39e7942c0:password123!
```