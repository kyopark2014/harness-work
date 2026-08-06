import os
import sys

from pdf2image import convert_from_path

# Rasterize at 200 DPI: high enough for legible text/vision-model input while
# keeping page images small; higher DPI mainly inflates size for this use case.
RENDER_DPI = 200
# Cap the longest edge at 1000px so downstream image consumers (e.g. multimodal
# LLM inputs) stay within token/size limits; larger pages are scaled down.
DEFAULT_MAX_DIMENSION_PX = 1000


def convert(pdf_path, output_dir, max_dim=DEFAULT_MAX_DIMENSION_PX):
    try:
        images = convert_from_path(pdf_path, dpi=RENDER_DPI)
    except Exception as e:
        print(f"Failed to convert PDF: {type(e).__name__}", file=sys.stderr)
        raise

    try:
        for i, image in enumerate(images):
            width, height = image.size
            if width > max_dim or height > max_dim:
                scale_factor = min(max_dim / width, max_dim / height)
                new_width = int(width * scale_factor)
                new_height = int(height * scale_factor)
                image = image.resize((new_width, new_height))

            image_path = os.path.join(output_dir, f"page_{i+1}.png")
            image.save(image_path)
            print(f"Saved page {i+1} as {image_path} (size: {image.size})")

        print(f"Converted {len(images)} pages to PNG images")
    except Exception as e:
        print(f"Failed while saving images: {type(e).__name__}", file=sys.stderr)
        raise


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: convert_pdf_to_images.py [input pdf] [output directory]")
        sys.exit(1)
    pdf_path = sys.argv[1]
    output_directory = sys.argv[2]
    try:
        convert(pdf_path, output_directory)
    except Exception:
        sys.exit(1)
