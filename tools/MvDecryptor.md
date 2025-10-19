## A lightweight CLI converter that decodes RPG MV game resources and turns off decoding in game config.

Sources were taken from here…  
<https://bitbucket.org/SilicaAndPina/rpgmv-decryptor/src/master/mvDecryptor.py> (origin source)
<https://github.com/kawanakaiku/RPGMV-DECRYPTOR>
…and patched for CLI usage and python3 compatibility (+MZ extensions).

Usage: either copy to the game folder and run:
```
./MvDecryptor
```

…or copy once to `/usr/local/bin/` and run in game folder (in terminal):
```
MvDecryptor
```

To use with an MZ game, create a folder named `www/` and move all other folders inside of it
before running the script (move them back and remove `www/` when you're done with those files).
