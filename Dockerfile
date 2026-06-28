# Use the official Microsoft Playwright Python image which has all browser dependencies pre-installed
FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

# Set working directory
WORKDIR /app

# Copy requirements and install
COPY job-agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the project
COPY . .

# Set working directory to job-agent
WORKDIR /app/job-agent

# Set environment variables for production/hosting
ENV HOST=0.0.0.0
ENV PORT=8765
ENV PYTHONUNBUFFERED=1

# Expose the application port
EXPOSE 8765

# Run the master orchestrator in web app mode
CMD ["python", "main.py"]
