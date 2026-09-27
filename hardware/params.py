"""Sizes for the desktop sprite, in millimetres.

Bought-part sizes cite their source; UNVERIFIED marks a number no maker publishes
(measure the part when it arrives). Everything else is a design choice.
"""

GAP = 0.5  # clearance between a bought part and whatever holds it

# --- Ball: ESP32-S3-Touch-AMOLED-1.75 under a glass dome ----------------------

BALL_OD = 60.0  # printed shell, outside diameter
BALL_WALL = 2.0
BALL_SEAM_Z = 0.0  # front ring / back shell split, above the shell centre
BALL_TILT = 15.0  # screen axis above horizontal when docked, degrees
BALL_GRILLE_PITCH, BALL_GRILLE_HOLE = 2.6, 1.2  # speaker holes at the back pole

# K9 optical dome: https://www.clzoptics.com/optical-dome2/ ; craft domes vary, measure yours
DOME_OD, DOME_HEIGHT, DOME_WALL = 50.0, 25.0, 2.0  # height 25 = full hemisphere

# Waveshare drawing, ESP32-S3-Touch-AMOLED-1.75-3D.zip on files.waveshare.com
AMOLED_GLASS_D = 48.96  # cover glass
AMOLED_ACTIVE_D = 43.76
AMOLED_GLASS_T = 2.05  # touch panel stack
AMOLED_PCB_D = 46.0  # round PCB
AMOLED_DEPTH = 10.4  # glass top to tallest part on the back, header pins excluded

BATTERY = (40.0, 30.0, 5.0)  # 503040 LiPo, ~600 mAh; buy Waveshare's MX1.25 lead
BALL_SPEAKER = (30.0, 20.0, 6.8)  # 2030 cavity speaker the kit ships with, UNVERIFIED

# --- Base: a slab leaning back, display in front, ball on a collar on top --------

WALL = 2.5
CORNER_R = 8.0
FACE_TILT = 12.0  # front and back faces lean back from vertical, degrees
CHIN_H = 30.0  # strip under the display, holds the radar
TOP_MARGIN = 12.0
SIDE_MARGIN = 8.0
BASE_DEPTH = 114.0  # front to back, horizontal; the Ø100 mic array sets it

COLLAR_OD, COLLAR_ID, COLLAR_H = 44.0, 32.0, 8.0  # the ring the ball sits in
SEAT_CLEARANCE = 0.5

# Touch Display 2 7" product brief, RP-010429-MM
DISPLAY_W, DISPLAY_H, DISPLAY_T = 189.32, 120.24, 14.92
DISPLAY_ACTIVE_W, DISPLAY_ACTIVE_H = 154.56, 86.94
DISPLAY_ACTIVE_DZ = 0.0  # active area assumed centred, UNVERIFIED

# Pi 5 mechanical drawing + Active Cooler brief; sits on the display's 8.5 standoffs
PI_W, PI_H = 85.0, 56.0
PI_T = 19.0  # board + cooler + USB/RJ45 stack, UNVERIFIED
PI_STANDOFF = 8.5
PI_DX = 12.7  # standoff grid is off the display centre by this much, side UNVERIFIED

# reSpeaker XVF3800 2D drawing + wiki AEC_MIC_ARRAY_GEO
MIC_D = 100.0  # round PCB
MIC_T = 7.4  # 1.2 PCB + 6.2 tallest part (3.5 mm jack)
MIC_XY = [(-33.0, -33.0), (33.0, -33.0), (-33.0, 33.0), (33.0, 33.0)]
MIC_HOLE = 1.5

# Waveshare 8 ohm 5 W speaker, SKU 14595 (one of a stereo pair), fires out the sides
SPEAKER_W, SPEAKER_H, SPEAKER_D = 100.0, 45.0, 21.0  # long side, short side, depth
GRILLE_LEN, GRILLE_WIDTH, GRILLE_PITCH, GRILLE_HOLE = 88.0, 36.0, 3.2, 1.8

# HLK-LD2450 manual; thickness UNVERIFIED (PCB + 5.0 back connector)
RADAR_W, RADAR_H, RADAR_T = 44.0, 15.0, 6.0
RADAR_GAP = 6.2  # antenna to cover: whole multiples of 6.2 per the manual

VENT_W, VENT_H, VENT_PITCH = 3.0, 40.0, 7.0
CABLE_W, CABLE_H = 30.0, 12.0
