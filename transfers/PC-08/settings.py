class SettingsManager:
    def __init__(self):
        self.rate_per_minute = 1.0
        self.high_contrast = False

    def set_rate(self, r: float):
        self.rate_per_minute = r

    def toggle_contrast(self):
        self.high_contrast = not self.high_contrast
