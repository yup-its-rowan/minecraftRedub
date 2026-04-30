### Minecraft Redub

## Day 1

Hey everyone and welcome to a new project I thought was easy and fun and doable when I thought of it but is actually much harder and more time consuming than I could have possibly imagined.

It all started yesterday, when I saw a mod for Minecraft where all the block textures were replaced by painted replicas. I thought that was so cool and interesting. 
Unforunately, I am not artistically inclined, so I instead decided to replace all the audio files with audio files of my own. 

I booted up the old PC and figured out the basics of resource pack creation, and after a half hour I felt comfortable enough to crack open the index file (where the list of audio files reside) and see what I have to look forwards to.

Boom. 4405 audio files. Not only would I have to record for weeks, I now also feel the need to automate the file generation and creation. The project has ballooned in size, but I'm no quitter.

First, I need to write a program that does a few things. I need to be able to open the index file and go to the most recent unchanged audio file. Then, I need to be able to listen to it, record the new noise, trim the resulting audio to match the old one, generate all relevant file structures in the assets folder, and then save the file in the correct format. Then I need to record that the file has already been created, and then move on to the next unchanged audio file. Ideally, I would have a visualization of the audio file above and a visualization of the recording right underneath, so I could see that the peaks and valleys are similar and what not. I also need the ability to re-record an audio if the recording is bad. I also need to avoid the sound of keypresses that toggle the recording itself.

And then I gotta do that 4 thousand times. Let's get to it.

## Day 2

This is a small update, just cleaned up the file structure and uploaded to git. I looked online to see if there is anything that does this already, like a program or a mod of some kind. But I can't seem to find anything. Cool!

Also renamed this from "SoundSwitchinator" to "MinecraftRedub". Maybe someone else looking to do the same thing can use this in the future :p



