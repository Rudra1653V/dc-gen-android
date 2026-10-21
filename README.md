# dc-gen-android

Semi-automatic Discord account generator. Drives the official Discord Android app
inside Android Studio AVDs on Apple Silicon. Ports DC-gen (macOS/Chrome/Selenium)
onto adb + uiautomator2 and keeps its behaviour, output formats and CLI vocabulary.

    python main.py --doctor
    python main.py --apk ~/apk/discord.apk --setup
    python main.py --recon --instances 0
    python main.py --dry-run -n 1 --instances 0 --human-mode assist
    caffeinate -dimsu python main.py -n 2 --batch 2 --human-mode assist

Outputs: tokens.txt, evs.txt, pending.txt, locked.txt, report.csv under outputs/.
Email verification is manual; SMS is optional and off by default.