import os
import argparse
import subprocess

def convert_audio(file_path, output_format, output_file=None):
    
    # Logic to determine output filename if not provided
    if output_file is None:
        if output_format == "mp3":
            output_file = f"{os.path.splitext(file_path)[0]}.mp3"
        else:
            output_file = f"{os.path.splitext(file_path)[0]}.mp4"

    if output_format == "mp3":
        command = [
            "ffmpeg",
            "-y",
            "-nostdin",
            "-v", "error",
            "-i", file_path,
            "-write_id3v1", "1",
            "-id3v2_version", "3",
            "-dither_method", "triangular",
            "-ar", "48000",
            "-qscale:a", "1",
            output_file
        ]
    else:
        command = [
            "ffmpeg",
            "-y", # Overwrite
            "-nostdin",
            "-v", "error", # Silence output
            "-i", file_path,
            "-c:a", "aac",
            "-b:a", "320k",
            "-dither_method", "rectangular",
            "-ar", "44100",
            output_file
        ]

    # ADDED: stdin=subprocess.DEVNULL ensures the subprocess cannot touch your keyboard input
    subprocess.run(command, check=True, stdin=subprocess.DEVNULL)

def process_directory(directory, output_format):
    for root, _, files in os.walk(directory):
        for file in sorted(files):
            if file.endswith((".wav", ".WAV", ".flac", ".WMA")):
                file_path = os.path.join(root, file)
                convert_audio(file_path, output_format)

def main():
    parser = argparse.ArgumentParser(description="Convert .wav or .flac files to .mp4 or .mp3 using ffmpeg.")
    parser.add_argument("-d", "--directory", required=True, help="Directory to process.")
    parser.add_argument("-f", "--format", choices=["mp4", "mp3"], default="mp4", help="Output format: mp4 (default) or mp3.")
    
    args = parser.parse_args()
    
    process_directory(args.directory, args.format)

if __name__ == "__main__":
    main()