import json
import logging
import sys

from PIL import Image, ImageDraw

logger = logging.getLogger(__name__)

BOUNDING_BOX_LINE_WIDTH = 2


def create_validation_image(page_number, fields_json_path, input_path, output_path):
    try:
        with open(fields_json_path, 'r') as f:
            data = json.load(f)

            img = Image.open(input_path)
            draw = ImageDraw.Draw(img)
            num_boxes = 0

            for field in data["form_fields"]:
                if field["page_number"] == page_number:
                    entry_box = field['entry_bounding_box']
                    label_box = field['label_bounding_box']
                    draw.rectangle(entry_box, outline='red', width=BOUNDING_BOX_LINE_WIDTH)
                    draw.rectangle(label_box, outline='blue', width=BOUNDING_BOX_LINE_WIDTH)
                    num_boxes += 2

            img.save(output_path)
            print(f"Created validation image at {output_path} with {num_boxes} bounding boxes")
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as e:
        logger.exception("Failed to create validation image")
        print("Error: Failed to create validation image", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) != 5:
        print("Usage: create_validation_image.py [page number] [fields.json file] [input image path] [output image path]")
        sys.exit(1)
    page_number = int(sys.argv[1])
    fields_json_path = sys.argv[2]
    input_image_path = sys.argv[3]
    output_image_path = sys.argv[4]
    create_validation_image(page_number, fields_json_path, input_image_path, output_image_path)
