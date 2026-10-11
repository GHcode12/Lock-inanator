import pygame

class SpriteSheet:
    def __init__(self, filename):
        # 1. Load the sprite sheet image safely with alpha transparency
        self.sheet = pygame.image.load(filename).convert_alpha()

    def get_image(self, frame_x, frame_y, width, height):
        """Cuts out a single frame rectangle from the master sheet."""
        image = pygame.Surface((width, height), pygame.SRCALPHA)
        image.blit(self.sheet, (0, 0), (frame_x, frame_y, width, height))
        return image

# Load your sprite sheet file
sprite_sheet = SpriteSheet("assets/Pixel Chibi Reading Animation Sprite Sheet.png")

# Define your measured frame size
FRAME_WIDTH = 64   # Replace with your actual calculated frame width
FRAME_HEIGHT = 64  # Replace with your actual calculated frame height

# Create a dictionary to hold the separated frame lists
animations = {
    "STANDING": [],
    "FLIPPING": [],
    "READING": [],
    "BLINKING": []
}

state_keys = ["STANDING", "FLIPPING", "READING", "BLINKING"]

# Loop through each row and column to slice frames automatically
for row_index, state_name in enumerate(state_keys):
    for col_index in range(8):  # 8 frames across per row
        x = col_index * FRAME_WIDTH
        y = row_index * FRAME_HEIGHT
        
        # Extract and append the frame surface to its respective state
        frame_image = sprite_sheet.get_image(x, y, FRAME_WIDTH, FRAME_HEIGHT)
        animations[state_name].append(frame_image)