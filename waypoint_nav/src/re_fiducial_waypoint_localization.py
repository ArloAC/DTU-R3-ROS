#!/usr/bin/env python

import rospy
import math
import json
from pyproj import Proj
from R3_functions import quat_rot, fit_in_rad, debug_info

# TF libraries
import tf
import tf2_ros
import tf2_msgs.msg
import geometry_msgs.msg

# ROS messages
from nav_msgs.msg import Odometry
from std_msgs.msg import String
from fiducial_msgs.msg import FiducialMapEntryArray, FiducialMapEntry, FiducialTransformArray

# Class
class fiducial_localization(object):
  def __init__(self):
    # Init ROS node
    rospy.init_node('fiducial_waypoint_localization')

    # rosparams
    self.robot_frame = rospy.get_param("~waypoint_control/base_frame", "base_footprint")
    self.gps_frame = rospy.get_param("~waypoint_control/gps_frame", "utm")
    self.camera_frame = rospy.get_param("~waypoint_control/camera_frame", "raspicam")
    self.distance_per_count = rospy.get_param("~driveGeometry/distancePerCount", "0.00338")
    self.fiducial_map_file = rospy.get_param("~waypoint_control/map_file", "Fiducials.json") 
    
    # Publishers
    self.robot_gps_pub = rospy.Publisher('robot_gps_pose', Odometry, queue_size = 10, latch = True)
    self.tf_pub = rospy.Publisher("tf", tf2_msgs.msg.TFMessage, queue_size=30, latch = True)
    self.state_pub = rospy.Publisher('waypoint/state', String, queue_size = 10, latch = True)
    self.debug_output = rospy.Publisher('debug_output', String, queue_size = 10)

    # Subscribers
    rospy.Subscriber('fiducial_map_gps', FiducialMapEntryArray, mapGPSCB)
    rospy.Subscriber('fiducial_transforms', FiducialTransformArray, transCB)
    rospy.Subscriber('serial', String, serialCB)
    rospy.Subscriber('waypoint/state', String, stateCB)

    # 1 Hz
    self.rate = rospy.Rate(1)
    
    # Variables
    self.projection = Proj(proj="utm", zone="34", ellps='WGS84')
    self.tfBuffer = tf2_ros.Buffer()
    self.tf_listener = tf2_ros.TransformListener(self.tfBuffer)
    self.reference_id = 0
    self.fiducial_gps_map = FiducialMapEntryArray()
    
    self.state = "STOP"
    self.prestate = "STOP"
    self.previous_fiducial = 0
    self.robot_stopped = True
    self.waiting_time = 0
    self.waiting = False
    self.l_counts = 0
    self.r_counts = 0
    self.l_displacement = 0
    self.r_displacement = 0
    
    # Init fiducials map in GPS from json, publish all fiducial to utm trans to tf 
    try:
      json_data = json.load(open(self.fiducial_map_file)) 
      # Save the map in FiducialMapEntryArray() 
      json_map = FiducialMapEntryArray()
      for fid in json_data["FiducialCollections"][0]["SavedFiducials"]: 
        fid_gps = FiducialMapEntry() 
        fid_gps.fiducial_id = fid["Id"] 
        fid_gps.x = fid["Position"]["longitude"] 
        fid_gps.y = fid["Position"]["latitude"] 
        fid_gps.z = fid["Position"]["altitude"] 
        fid_gps.rx = fid["Rotation"]["east"] 
        fid_gps.ry = fid["Rotation"]["north"] 
        fid_gps.rz = fid["Rotation"]["heading"] 
        json_map.fiducials.append(fid_gps)
      self.fiducial_gps_map = json_map
      debug_info(self.debug_output, "Fiducial map initialised")
    except:
      debug_info(self.debug_output, "Initialise map from json file failed")
      
  def Start(self):
    while not rospy.is_shutdown():
      self.rate.sleep()
  
  # Service client
  def QuatRot_client(q, deg_x, deg_y, deg_z):
    rospy.wait_for_service('QuatRot')
    try:
      quat_rot = rospy.ServiceProxy('QuatRot', QuatRot)
      resp = quat_rot(q, deg_x, deg_y, deg_z)
      return resp.quat
    except rospy.ServiceException, e:
      rospy.logwarn("Service call failed: %s"%e)
      
  # ROS callback function
  def mapGPSCB(GPS_map):
    self.fiducial_gps_map = GPS_map
    debug_info(self.debug_output, "Fiducial map updated")
    
  def transCB(t):
    prev_in_view = False
    reference_in_view = False
    for fid_trans in t.transforms:  
      # Check whether the detected fiducial is still in the view
      if fid_trans.fiducial_id == self.reference_id: 
        reference_in_view = True
      
      # Check whether the previous fiducial is out of the view
      if fid_trans.fiducial_id == self.previous_fiducial: 
        prev_in_view = True
        # if the displacement of both wheels is small
        if (self.l_displacement * float(self.distance_per_count) < 3.0) and (self.r_displacement * float(self.distance_per_count) < 3.0):
          return 
    
    # Clear the previous fiducial    
    if not prev_in_view:
      self.previous_fiducial = 0
    
    # If the detected is out of view when the robot stops, continue
    if self.waiting and not reference_in_view:
      # Cotinue move to the waypoint
      state_msg = String()
      state_msg.data = self.prestate
      self.state_pub.publish(state_msg)         
      self.waiting = False
    
    for fid_trans in t.transforms:
      # Check the image error
      if fid_trans.image_error > 0.3:
        return   
      
      # Check whether detected fiducial is in the map  
      for fid_gps in fiducial_gps_map.fiducials: 
        # Only process the detected fiducial in the map
        if fid_trans.fiducial_id != fid_gps.fiducial_id:
          continue
       
        self.reference_id = fid_trans.fiducial_id
        fid_name = str(self.reference_id)
      
        # Pause the navigation and stop the robot
        if not self.waiting:
          self.prestate = self.state
        state_msg = String()
        state_msg.data = "STOP"
        self.state_pub.publish(state_msg)
        self.waiting = True
      
        # wait until robot stops
        if not self.robot_stopped:
          # Initialise the waiting time
          self.waiting_time = 0
          self.rate.sleep()
          return

        # Wait another 5 seconds for updating the image
        if self.waiting_time < 5:
          self.waiting_time += 1
          self.rate.sleep()
          return
        
        # Publish tf from fid to camera
        tf_fid_cam = geometry_msgs.msg.TransformStamped()
        tf_fid_cam.header.frame_id = self.camera_frame
        tf_fid_cam.child_frame_id = fid_name
        tf_fid_cam.header.stamp = rospy.Time.now()      
        tf_fid_cam.transform = fid_trans.transform
        tfmsg_fid_cam = tf2_msgs.msg.TFMessage([tf_fid_cam])
        self.tf_pub.publish(tfmsg_fid_cam)
        if self.robot_stopped:
          self.rate.sleep()

        # Publish tf from fid to utm
        tf_fid_utm = geometry_msgs.msg.TransformStamped()
        tf_fid_utm.header.frame_id = self.gps_frame
        tf_fid_utm.child_frame_id = "fiducial"+fid_name
        tf_fid_utm.header.stamp = rospy.Time.now() 
        tf_fid_utm.transform.translation.x, tf_fid_utm.transform.translation.y = self.projection(fid_gps.x, fid_gps.y)
        tf_fid_utm.transform.translation.z = fid_gps.z
        quat = tf.transformations.quaternion_from_euler(-math.radians(fid_gps.rx), -math.radians(fid_gps.ry), -math.radians(fid_gps.rz))
        tf_fid_utm.transform.rotation.x = quat[0] 
        tf_fid_utm.transform.rotation.y = quat[1] 
        tf_fid_utm.transform.rotation.z = quat[2] 
        tf_fid_utm.transform.rotation.w = quat[3] 
        tfmsg_fid_utm = tf2_msgs.msg.TFMessage([tf_fid_utm])
        self.tf_pub.publish(tfmsg_fid_utm)
        if not self.robot_stopped:
          self.rate.sleep()

        # Publish tf from robot to fid
        try: 
          robot_fid_trans = self.tfBuffer.lookup_transform(self.fid_name, self.robot_frame, rospy.Time())
          tf_robot_fid = geometry_msgs.msg.TransformStamped()
          tf_robot_fid.header.frame_id = "fiducial"+fid_name
          tf_robot_fid.child_frame_id = "robot_fid"
          tf_robot_fid.header.stamp = rospy.Time.now()      
          tf_robot_fid.transform = robot_fid_trans.transform
          tfmsg_robot_fid = tf2_msgs.msg.TFMessage([tf_robot_fid])
          self.tf_pub.publish(tfmsg_robot_fid)        
          if self.robot_stopped:
            self.rate.sleep()
        except (tf2_ros.LookupException, tf2_ros.ConnectivityException, tf2_ros.ExtrapolationException):
          return
      
        # Publish tf from robot to utm
        try:
          verify_trans = self.tfBuffer.lookup_transform(self.gps_frame, "fiducial"+fid_name, rospy.Time())          
          verify_trans2 = self.tfBuffer.lookup_transform("fiducial"+fid_name, "robot_fid", rospy.Time())
          robot_utm_trans = self.tfBuffer.lookup_transform(self.gps_frame, "robot_fid", rospy.Time())
          robot_gps_pose = Odometry()
          robot_gps_pose.pose.pose.position.z = robot_utm_trans.transform.translation.z
          robot_gps_pose.pose.pose.position.x,robot_gps_pose.pose.pose.position.y = self.projection(robot_utm_trans.transform.translation.x, robot_utm_trans.transform.translation.y, inverse=True)
          robot_gps_pose.pose.pose.orientation.x = -robot_utm_trans.transform.rotation.x
          robot_gps_pose.pose.pose.orientation.y = -robot_utm_trans.transform.rotation.y
          robot_gps_pose.pose.pose.orientation.z = -robot_utm_trans.transform.rotation.z
          robot_gps_pose.pose.pose.orientation.w = robot_utm_trans.transform.rotation.w
          robot_gps_pose.pose.pose.orientation = quatRot(robot_gps_pose.pose.pose.orientation,0,0,90)
          self.robot_gps_pub.publish(robot_gps_pose)         
          # Resume the navigation when the update is done
          state_msg = String()
          state_msg.data = prestate
          self.state_pub.publish(state_msg)         
          self.waiting = False 
          self.previous_fiducial = reference_id
          self.l_displacement = 0
          self.r_displacement = 0
        except (tf2_ros.LookupException, tf2_ros.ConnectivityException, tf2_ros.ExtrapolationException):    
          return

  def stateCB(s):
    self.state = s.data        
  
  def serialCB(s):
    if len(s.data) <= 0:
      return 
    line_parts = s.data.split('\t')
    try:
      v = float(line_parts[5])
      omega = float(line_parts[6])
      new_l_counts = int(line_parts[7])
      new_r_counts = int(line_parts[8])
      # Check whether the robot stops
      if math.fabs(v) < 0.05 and math.fabs(omega) < 0.05:
        self.robot_stopped = True
      else:
        self.robot_stopped = False  
      self.l_displacement = self.l_displacement + math.fabs(new_l_counts - self.l_counts)
      self.r_displacement = self.r_displacement + math.fabs(new_r_counts - self.r_counts)
      self.l_counts = new_l_counts
      self.r_counts = new_r_counts
    except:
      return
  
if __name__ == '__main__': 
  fid = fiducial_localization() 
  fid.Start()
  