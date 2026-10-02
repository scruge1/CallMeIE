"""Static tenant demo assets. No telephony or knowledge publishing API."""
from pathlib import Path
from fastapi.responses import FileResponse

def install(app):
    @app.get('/client/great-national-demo.js', include_in_schema=False)
    def javascript():
        return FileResponse(Path(__file__).with_name('great-national-demo.js'), media_type='application/javascript', headers={'Cache-Control':'no-cache'})

    @app.get('/client/great-national-demo.css', include_in_schema=False)
    def stylesheet():
        return FileResponse(Path(__file__).with_name('great-national-demo.css'), media_type='text/css', headers={'Cache-Control':'no-cache'})
