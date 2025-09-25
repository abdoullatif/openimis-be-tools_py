import os
from setuptools import find_packages, setup

with open(os.path.join(os.path.dirname(__file__), "README.md")) as readme:
    README = readme.read()

# allow setup.py to be run from any path
os.chdir(os.path.normpath(os.path.join(os.path.abspath(__file__), os.pardir)))

setup(
    name="openimis-be-tools",
    version='1.5.1',
    packages=find_packages(),
    include_package_data=True,
    license="GNU AGPL v3",
    description="The openIMIS Backend Tools reference module.",
    # long_description=README,
    url="https://openimis.org/",
    author="Bluesquare",
    author_email="developers@bluesquare.org",
    install_requires=[
        "django",
        "django-db-signals",
        "djangorestframework",
        "djangorestframework-xml",
        "simplejson",
        "zipfile36",
        #"pyminizip",
        "pyzipper",
        "defusedxml",
        "tablib",
        "django-import-export",
    ],
    classifiers=[
        "Environment :: Web Environment",
        "Framework :: Django",
        "Framework :: Django :: 2.1",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: GNU Affero General Public License v3",
        "Operating System :: OS Independent",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
    ],
)
