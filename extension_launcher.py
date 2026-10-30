import os
from seleniumbase import Driver

profile_dir = os.path.abspath("./chrome_profile_buster")
driver = Driver(uc=True, user_data_dir=profile_dir, headless=False)

# Open the Chrome Web Store
driver.get("https://chromewebstore.google.com")

# Keep the browser open so you can search and install extensions
input("Install your desired extensions in the browser window, then press Enter here to close: ")
driver.quit()