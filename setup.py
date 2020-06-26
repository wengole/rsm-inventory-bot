from setuptools import setup

install_requires = [
    "aiohttp==3.6.2",
    "black==19.10b0",
    "discord.py==1.3.3",
    "EsiPy==1.0.0",
    "dynaconf==2.2.3",
    "redis==3.5.3",
    "requests==2.24.0",
    "dhooks==1.1.3",
]

setup(
    name="rsm_inventory_bot",
    version="0.0.1",
    packages=[""],
    url="https://github.com/wengole/rsm_inventory_bot",
    license="",
    author="Ben Cole",
    author_email="wengole@gmail.com",
    description="",
    install_requires=install_requires,
)
