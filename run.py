if __name__ == "__main__":
    # Imported here, not at module level: the spawn child re-runs this file as __mp_main__.
    from edge_api import create_app

    app = create_app()
    try:
        app.run(host=app.config['services'].config.bind_host, port=5000)
    finally:
        app.config['services'].runtime.stop()
