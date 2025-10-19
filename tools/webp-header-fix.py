#!/usr/bin/env python3
# webp corrupted file header repair

import os
import struct
import sys
b = os.path.getsize(sys.argv[1])-8
c = struct.pack('<I', b)
# fixed for Python3
offset = 0o4
with open(sys.argv[1], 'r+b') as f:
    f.seek(offset)
    f.write(c)
