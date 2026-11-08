#!/bin/zsh
# Build ~/Applications/Morpheus.app and start it at login (LaunchAgent).
# Rebuilding changes the app's signature, so macOS asks for its permissions again:
# only rerun after changing launcher.c or this file, not after Python changes
# (for those, `morpheus restart` is enough).
set -e
HERE=${0:A:h}
DIR=${HERE:h}
APP=~/Applications/Morpheus.app
LABEL=ng.forgegrowth.morpheus
PLIST=~/Library/LaunchAgents/$LABEL.plist

mkdir -p $APP/Contents/MacOS $APP/Contents/Resources ~/Applications ~/Library/Logs
cp $DIR/assets/Morpheus.icns $APP/Contents/Resources/Morpheus.icns
clang -O2 -DPYTHON="\"$DIR/.venv/bin/python\"" -DDIR="\"$DIR\"" -o $APP/Contents/MacOS/Morpheus $HERE/launcher.c
cat > $APP/Contents/Info.plist <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleIdentifier</key><string>$LABEL</string>
  <key>CFBundleName</key><string>Morpheus</string>
  <key>CFBundleExecutable</key><string>Morpheus</string>
  <key>CFBundleIconFile</key><string>Morpheus</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSUIElement</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>Morpheus listens for "Hey Morpheus" and your commands. Speech is transcribed on this Mac.</string>
  <key>NSAppleEventsUsageDescription</key><string>Morpheus controls apps when you ask it to.</string>
</dict></plist>
PL
codesign --force --sign - $APP

cat > $PLIST <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$APP/Contents/MacOS/Morpheus</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/Morpheus.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/Morpheus.log</string>
</dict></plist>
PL
launchctl bootout gui/$(id -u)/$LABEL 2>/dev/null || true
for i in {1..20}; do   # bootout returns before the old copy is gone; bootstrap fails until it is
  launchctl print gui/$(id -u)/$LABEL >/dev/null 2>&1 || break
  sleep 0.5
done
launchctl bootstrap gui/$(id -u) $PLIST
mkdir -p ~/.local/bin
ln -sf $HERE/morpheus ~/.local/bin/morpheus
echo "Installed $APP, starts at login. Control it with: morpheus status|pause|restart|stop|start|log"
