### Build instructions (Mono)

After downloading and unpacking [the sources][sources], apply [the patch][patch] and build the Mono executable:
```bash
# run from the sources folder, after copying patch file there
patch --strip=1 < RgssDecrypter-v1.0.0.1_linux.patch
mcs -out:RgssDecrypter.exe -recurse:'*.cs'
```

The patch deletes `RgssDecrypter.Lib/Properties/`, and modifies line #103 in `RgssDecrypter/Program.cs`:
```csharp
var targetPath = Path.Combine(outDir, pointer.Name.Replace('\\', '/'));
```
(this changes backslashes to slashes in generated paths – otherwise the paths don't get split into subfolders)
It also modifies the program to print out a message and return failure status if a botched archive is detected.

[sources]: https://github.com/usagirei/RGSS-Decryptor/releases
[patch]: rgss-decryptor/RgssDecrypter-v1.0.0.1.patch
