from pathlib import Path
from PIL import Image, ImageDraw
S = 512
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
grad = Image.new("RGB", (1, S))
top, bottom = (255, 155, 82), (236, 104, 38)
for y in range(S):
    t = y / (S - 1)
    grad.putpixel((0, y), tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
grad = grad.resize((S, S))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=118, fill=255)
img.paste(grad, (0, 0), mask)
d = ImageDraw.Draw(img)
# book
d.rounded_rectangle([96, 150, 416, 386], radius=30, fill=(255, 253, 247, 255))
d.rectangle([249, 150, 263, 386], fill=(228, 200, 168, 255))
for i, y in enumerate((238, 278, 318)):
    w = (112, 96, 74)[i]
    d.rounded_rectangle([130, y, 130 + w, y + 15], radius=7, fill=(214, 188, 158, 255))
    d.rounded_rectangle([382 - w, y, 382, y + 15], radius=7, fill=(214, 188, 158, 255))
# bookmark ribbon
d.polygon([(306, 150), (348, 150), (348, 246), (327, 222), (306, 246)], fill=(42, 165, 160, 255))
folder = Path(__file__).resolve().parent
folder.mkdir(exist_ok=True)
img.resize((256, 256), Image.Resampling.LANCZOS).save(folder / "storybook.png")
img.save(folder / "storybook.ico", sizes=[(256,256),(128,128),(64,64),(48,48),(32,32),(16,16)])
img.resize((64, 64), Image.Resampling.LANCZOS).save(folder.parent / "web" / "favicon.png")
print("Application icons generated")
