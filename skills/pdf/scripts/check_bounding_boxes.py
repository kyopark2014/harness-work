from dataclasses import dataclass
import json
import sys

MAX_ERROR_MESSAGES = 20

@dataclass
class RectAndField:
    rect: list[float]
    rect_type: str
    field: dict


def get_bounding_box_messages(fields_json_stream) -> list[str]:
    messages = []
    fields = json.load(fields_json_stream)
    messages.append(f"Read {len(fields['form_fields'])} fields")

    def rects_intersect(rect1, rect2):
        disjoint_horizontal = rect1[0] >= rect2[2] or rect1[2] <= rect2[0]
        disjoint_vertical = rect1[1] >= rect2[3] or rect1[3] <= rect2[1]
        return not (disjoint_horizontal or disjoint_vertical)

    rects_and_fields = []
    for f in fields["form_fields"]:
        rects_and_fields.append(RectAndField(f["label_bounding_box"], "label", f))
        rects_and_fields.append(RectAndField(f["entry_bounding_box"], "entry", f))

    has_error = False
    for i, rect_i in enumerate(rects_and_fields):
        for j in range(i + 1, len(rects_and_fields)):
            rect_j = rects_and_fields[j]
            if rect_i.field["page_number"] == rect_j.field["page_number"] and rects_intersect(rect_i.rect, rect_j.rect):
                has_error = True
                if rect_i.field is rect_j.field:
                    messages.append(f"FAILURE: intersection between label and entry bounding boxes for `{rect_i.field['description']}` ({rect_i.rect}, {rect_j.rect})")
                else:
                    messages.append(f"FAILURE: intersection between {rect_i.rect_type} bounding box for `{rect_i.field['description']}` ({rect_i.rect}) and {rect_j.rect_type} bounding box for `{rect_j.field['description']}` ({rect_j.rect})")
                if len(messages) >= MAX_ERROR_MESSAGES:
                    messages.append("Aborting further checks; fix bounding boxes and try again")
                    return messages
        if rect_i.rect_type == "entry":
            if "entry_text" in rect_i.field:
                font_size = rect_i.field["entry_text"].get("font_size", 14)
                entry_height = rect_i.rect[3] - rect_i.rect[1]
                if entry_height < font_size:
                    has_error = True
                    messages.append(f"FAILURE: entry bounding box height ({entry_height}) for `{rect_i.field['description']}` is too short for the text content (font size: {font_size}). Increase the box height or decrease the font size.")
                    if len(messages) >= MAX_ERROR_MESSAGES:
                        messages.append("Aborting further checks; fix bounding boxes and try again")
                        return messages

    if not has_error:
        messages.append("SUCCESS: All bounding boxes are valid")
    return messages

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: check_bounding_boxes.py [fields.json]")
        sys.exit(1)
    with open(sys.argv[1]) as f:
        messages = get_bounding_box_messages(f)
    for msg in messages:
        print(msg)
