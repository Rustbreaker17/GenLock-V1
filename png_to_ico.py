from PIL import Image
import sys, os

# change input name if needed
input_file = "lock.png"
output_file = "lock.ico"

if not os.path.exists(input_file):
    print(f"Input file not found: {input_file}")
    sys.exit(1)

# open image
img = Image.open(input_file).convert("RGBA")

# preferred icon sizes for Windows
sizes = [(256,256),(128,128),(64,64),(48,48),(32,32),(16,16)]

# save as .ico with multiple sizes
img.save(output_file, format='ICO', sizes=sizes)
print("Saved:", output_file)
