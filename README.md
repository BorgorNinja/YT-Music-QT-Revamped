# YT-Music-QT

YT-Music-QT is a Python application that allows users to search for and stream music from YouTube. The application consists of a client interface built with PyQt5 and a backend server that handles music search and streaming functionalities.

## Features

- Search for music on YouTube.
- Stream audio directly from YouTube.
- Clickable results for easy navigation.
- Media controls that fade after a song finishes.
- Recommendations for similar songs based on genre and artist.

## Project Structure

```
ytmusic-streamer
├── src
│   ├── client
│   │   ├── main.py        # Entry point of the client application
│   │   ├── player.py      # Handles media playback functionality
│   │   └── ui.py          # Manages user interface components
│   ├── backend
│   │   ├── backend.py     # Core backend functionality for music search and streaming
│   │   ├── server.py      # Sets up the server for handling client requests
│   │   └── config.py      # Configuration settings for the backend server
│   └── __init__.py        # Marks the src directory as a Python package
├── requirements.txt        # Lists project dependencies
├── .env.example            # Template for environment variables
├── .gitignore              # Specifies files to ignore in Git
└── README.md               # Documentation for the project
```

## Installation

1. Clone the repository:
   ```
   git clone <repository-url>
   cd YT-Music-QT-Revamped-main
   ```

2. Install the required dependencies:
   ```
   pip install -r requirements.txt
   ```

3. Configure the backend server settings in `config.py` to set the desired IP address and port.

4. (Optional) Copy `.env.example` to `.env` and modify it to set environment variables as needed.

## Usage

1. Start the backend server:
   ```
   python server.py
   ```

2. Run the client application:
   ```
   python main.py
   ```

3. Use the search field to find music and click on the results to start streaming.

## Contributing

Contributions are welcome! Please open an issue or submit a pull request for any enhancements or bug fixes.

## License

This project is licensed under the GNU GPL 2.0 License. See the LICENSE file for details.
