# GitHub Upload Steps

Recommended repository name:

```text
houston-311-road-issue-hotspots
```

## Option 1: Upload With GitHub Website

1. Create a new public repository on GitHub named `houston-311-road-issue-hotspots`.
2. Do not initialize it with a README because this folder already has one.
3. Upload the project files from this folder.
4. Do not upload anything inside `data/raw/`; those files are large and are intentionally ignored.

## Option 2: Upload With Git

From inside this project folder:

```bash
git init
git add .
git commit -m "Add Houston 311 road issue hotspot analysis"
git branch -M main
git remote add origin https://github.com/irum13/houston-311-road-issue-hotspots.git
git push -u origin main
```

If Git asks for your name and email:

```bash
git config user.name "Irum Naureen"
git config user.email "your-email@example.com"
```

Use the email you want associated with GitHub commits.

