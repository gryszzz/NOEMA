TEV — NOEMA trading room for macOS
==================================
TEV.app is a small native app (Swift + WebKit): the dashboard opens in its own window,
no browser tabs or address bar. The view is read-only.

1. Unzip TEV-mac.zip.
2. Drag TEV.app into /Applications (and into the Dock if you like).
3. First launch: right-click TEV > Open > Open (the app is ad-hoc signed, so macOS asks once).
4. Set your dashboard URL on the first line of ~/.tev-url (recommended; survives app updates), e.g.
     echo 'https://<your-machine>.<your-tailnet>.ts.net/trading-room' > ~/.tev-url
   or edit TEV.app/Contents/Resources/tev-url.txt (right-click TEV > Show Package Contents).
   If neither is set, TEV shows a dialog telling you where to put it.
5. Click TEV and log in with the dashboard user/password you were given (saved to your keychain).
   Cmd+R reloads. Links to other sites open in your normal browser.

Build from source (macOS, Xcode command line tools): apps/macos/build.sh
